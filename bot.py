import os
import io
import sys
import json
import base64
import threading
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    print(f"✅ knowledge.json chargé pour {KNOWLEDGE.get('agence')}")
except Exception as e:
    # On échoue vite et clairement plutôt que de continuer avec KNOWLEDGE=None
    # et planter plus tard sur un TypeError obscur (NoneType is not subscriptable)
    sys.exit(f"ERREUR FATALE: knowledge.json invalide — {e}")

menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

async def safe_reply(update: Update, text: str):
    """Le prompt Gemini demande du **gras** — on essaie en Markdown, et si Telegram
    rejette la syntaxe (caractères non échappés dans un prix/texte), on retombe sur
    du texte brut plutôt que de planter silencieusement sans répondre."""
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text, reply_markup=reply_markup)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - KomaraBot is running")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

async def ask_gemini_with_knowledge(question: str) -> str:
    knowledge_str = json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2) if KNOWLEDGE else "{}"

    system_prompt = (
        f"Tu es KomaraBot, l'assistant commercial de {KNOWLEDGE['agence']} {KNOWLEDGE['pays']}.\n"
        f"Slogan: {KNOWLEDGE['slogan']}\nMission: {KNOWLEDGE['mission']}\n\n"
        f"BASE DE CONNAISSANCES COMPLÈTE:\n{knowledge_str}\n\n"
        "CAPACITÉ SPÉCIALE DU BOT (très important):\n"
        "Tu peux générer une VRAIE image de démonstration directement dans ce chat Telegram. "
        "Si le client veut voir un exemple, une démo, ou un aperçu visuel, dis-lui de taper "
        "'/genere' suivi d'une description (ex: /genere affiche pour un restaurant africain moderne). "
        "Ne le renvoie JAMAIS vers un humain pour ça — c'est une fonctionnalité que TU as.\n\n"
        "MISSION:\n"
        "1. Sois le meilleur commercial. Accueille, conseille, et vends les services.\n"
        "2. Réponds d'abord avec les infos de la BASE DE CONNAISSANCES.\n"
        "3. Si info manquante (hors démo image), dis: 'Je vous mets en contact avec un expert' et donne le WhatsApp.\n"
        "RÈGLES:\n"
        "- Français professionnel, vouvoiement\n"
        "- Réponse courte, structurée avec **gras** et listes •\n"
        "- 2 émojis max\n"
        "- Termine par un appel à l'action: 'Voulez-vous un devis?' ou 'Contact: +212701986219'\n"
        "- Cite le slogan 1 fois sur 3"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\nQuestion du client: {question}"}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 1000},
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join([p["text"] for p in parts if "text" in p]).strip()
    except Exception as e:
        return f"Désolé, une erreur est survenue. Contactez-nous directement: {KNOWLEDGE['contact']['whatsapp']}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte = f"""Salut! Je suis KomaraBot 🇬🇳
Assistant officiel de {KNOWLEDGE['agence']}
{KNOWLEDGE['slogan']}

Je peux vous aider à:
• Générer des images IA
• Avoir un devis pour site web/logo
• Connaître nos services

Commandes disponibles:
/genere + description → créer une image IA
/exemples → voir nos réalisations

Ou tapez sur un bouton ci-dessous 👇"""
    await update.message.reply_text(texte, reply_markup=reply_markup)

# Mots-clés qui indiquent une demande d'image même sans /genere explicite.
IMAGE_KEYWORDS = [
    "génère", "genere", "générer", "generer", "crée une image", "créer une image",
    "cree une image", "creer une image", "fais une image", "fait une image",
    "photo de", "image de", "logo pour", "logo de", "affiche pour", "affiche de",
    "visuel pour", "visuel de", "design pour", "montage photo", "colorisation",
    "coloriser", "photoréaliste", "photorealiste", "upscale", "rendu final",
    "arrière-plan", "background", "retouche photo",
]

def _should_generate_image(text: str) -> bool:
    """Règle de routage: prompt long (>25 mots) OU mots-clés précis -> génération
    d'image automatique, même si le client n'a pas tapé /genere."""
    if len(text.split()) > 25:
        return True
    lowered = text.lower()
    return any(k in lowered for k in IMAGE_KEYWORDS)

async def generate_and_send_image(update: Update, prompt: str):
    """Génère et envoie l'image en suivant STRICTEMENT le prompt du client —
    on n'y ajoute que des mots-clés de qualité, jamais de style imposé qui
    écraserait son intention réelle."""
    await update.message.reply_text("Création de votre visuel IA en cours... ⏳ 20s", reply_markup=reply_markup)

    # Pollinations.ai — gratuit, sans clé API. httpx async: rien ne bloque l'event loop.
    clean_prompt = prompt.strip()
    enhanced_prompt = f"{clean_prompt}, high quality, professional, detailed, 4k"
    image_url = f"https://image.pollinations.ai/prompt/{quote(enhanced_prompt)}?width=1024&height=1024&nologo=true&seed={abs(hash(clean_prompt)) % 1000000}"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(image_url, timeout=60.0)
        response.raise_for_status()
        photo = io.BytesIO(response.content)
        photo.name = "komara_image.png"
        await update.message.reply_photo(
            photo,
            caption="Voici votre visuel selon votre description.\n\nVoulez-vous l'utiliser pour une pub? Contactez-nous.",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur génération image: {e}")
        await update.message.reply_text(
            f"Désolé, la génération a échoué. Réessayez avec une description plus courte, "
            f"ou contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )

async def genere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Ex: /genere affiche publicitaire pour Komara Agency", reply_markup=reply_markup)
        return
    await generate_and_send_image(update, prompt)

async def exemples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie les photos d'exemples stockées dans le dossier examples/ du repo."""
    # Lister les images dans examples/
    valid_exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        image_files = [
            f for f in os.listdir(EXAMPLES_DIR)
            if f.lower().endswith(valid_exts) and os.path.isfile(os.path.join(EXAMPLES_DIR, f))
        ]
    except Exception:
        image_files = []

    if not image_files:
        await update.message.reply_text(
            "Aucun exemple disponible pour le moment. 📸\n"
            "Nos réalisations arrivent bientôt !\n\n"
            f"En attendant, contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
        return

    await update.message.reply_text(
        f"Voici quelques réalisations de {KNOWLEDGE['agence']} 📸",
        reply_markup=reply_markup,
    )

    # Envoyer chaque image (max 5 pour éviter le spam)
    for img_name in image_files[:5]:
        img_path = os.path.join(EXAMPLES_DIR, img_name)
        try:
            with open(img_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"Réalisé par {KNOWLEDGE['agence']} ✨",
                )
        except Exception as e:
            print(f"⚠️ Erreur envoi {img_name}: {e}")

    await update.message.reply_text(
        "Voulez-vous un projet similaire? Tapez /genere + votre idée pour un visuel sur-mesure. 🎨",
        reply_markup=reply_markup,
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        if text == "Vos services":
            reponse = await ask_gemini_with_knowledge("Présente tous les services de Komara Agency avec prix et délais de façon commerciale.")
        elif text == "Tarifs":
            reponse = await ask_gemini_with_knowledge("Donne la liste des tarifs de tous les services.")
        elif text == "Commander":
            reponse = f"Parfait! Décrivez votre projet ici ou contactez directement notre équipe sur WhatsApp: {KNOWLEDGE['contact']['whatsapp_lien']}"
        elif text == "Contact":
            c = KNOWLEDGE['contact']
            reponse = f"📞 **Contactez {KNOWLEDGE['agence']}**\n\nWhatsApp: {c['whatsapp']}\nEmail: {c['email']}\nAdresse: {c['adresse']}\nPortfolio: {c['portfolio']}\n\n{KNOWLEDGE['slogan']}"
        elif text == "Portfolio":
            reponse = f"Découvrez nos réalisations ici 👇\n{KNOWLEDGE['contact']['portfolio']}"
        elif text == "Vidéo IA":
            reponse = await ask_gemini_with_knowledge("Parle du service Vidéo IA, prix et délais.")
        elif _should_generate_image(text):
            # Prompt long (>25 mots) ou mots-clés précis -> génération d'image
            # directe, sans exiger que le client tape /genere.
            await generate_and_send_image(update, text)
            return
        else:
            reponse = await ask_gemini_with_knowledge(text)
        await safe_reply(update, reponse)
    except Exception as e:
        # Filet de sécurité: le bot ne doit JAMAIS rester silencieux, même en cas
        # de bug imprévu. On log l'erreur et on répond quand même au client.
        print(f"⚠️ Erreur dans handle_menu: {e}")
        try:
            await update.message.reply_text(
                f"Désolé, un problème technique est survenu. 🙏 Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
                reply_markup=reply_markup,
            )
        except Exception:
            pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Filet de sécurité ultime au niveau de l'application: si un handler plante
    d'une façon totalement imprévue (bug non couvert par les try/except internes),
    le bot répond quand même au client au lieu de rester silencieux."""
    print(f"⚠️ Erreur non gérée par l'application: {context.error}")
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"Désolé, un problème technique est survenu. 🙏 Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
                reply_markup=reply_markup,
            )
    except Exception:
        pass

def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("genere", genere))
    app.add_handler(CommandHandler("exemples", exemples))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_error_handler(error_handler)
    print(f"Bot {KNOWLEDGE['agence']} lancé...")
    # run_polling() gère lui-même le nettoyage du webhook (delete_webhook) avant
    # de démarrer — pas besoin d'appeler asyncio.run() nous-mêmes ici : ça créait
    # et fermait une boucle asyncio séparée, ce qui cassait la boucle que
    # run_polling() essaie ensuite de récupérer ("no current event loop").
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
