import os
import io
import sys
import json
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 1. LIRE LES CLÉS DEPUIS RAILWAY (Ne plus les mettre en dur ici !)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Vérification au démarrage — crash clair plutôt qu'erreur cryptique plus tard
if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant dans les variables d'environnement Railway.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant dans les variables d'environnement Railway.")

# 2. CHARGER LA BASE DE CONNAISSANCES
KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    print(f"✅ knowledge.json chargé ({len(KNOWLEDGE.get('services', []))} services)")
except FileNotFoundError:
    print("⚠️ knowledge.json introuvable — le bot fonctionnera sans base de connaissances")
    KNOWLEDGE = None
except json.JSONDecodeError as e:
    print(f"⚠️ knowledge.json invalide: {e}")
    KNOWLEDGE = None

# Menu clavier — "Vos services" ajouté
menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

# --- Mini serveur HTTP pour le health check Railway ---
# Railway exige que l'app écoute sur $PORT, sinon le déploiement est marqué "failed".
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - KomaraBot is running")

    def log_message(self, format, *args):
        pass  # silence les logs HTTP inutiles


def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

# --- Appel à Gemini (texte) avec la base de connaissances ---

async def ask_gemini_with_knowledge(question: str) -> str:
    """
    Envoie la question à Gemini avec la base de connaissances en contexte.
    Retourne une réponse textuelle propre.
    """
    knowledge_str = json.dumps(KNOWLEDGE, ensure_ascii=False) if KNOWLEDGE else "{}"

    system_prompt = (
        "Tu es Komara Agency, une agence digitale basée en Guinée. "
        "Réponds aux questions des clients en utilisant UNIQUEMENT ces informations:\n\n"
        f"{knowledge_str}\n\n"
        "Règles:\n"
        "- Réponds de manière claire, professionnelle et concise\n"
        "- Si l'info n'est pas dans la base, dis-le honnêtement\n"
        "- Utilise des émojis légèrement pour un ton chaleureux\n"
        "- Réponds toujours en français"
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash-exp:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{system_prompt}\n\nQuestion du client: {question}"}]}
        ],
        "generationConfig": {
            "responseModalities": ["TEXT"],
            "temperature": 0.7,
            "maxOutputTokens": 800,
        },
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()

    parts = data["candidates"][0]["content"]["parts"]
    text_response = ""
    for part in parts:
        if "text" in part:
            text_response += part["text"]

    return text_response.strip() or "Désolé, je n'ai pas pu générer une réponse."

# --- Handlers Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte = """Salut! Je suis KomaraBot 🇬🇳
Je transforme tes idées en images IA.
Utilise: /genere + ta description
Ex: /genere logo Komara Agency moderne"""
    await update.message.reply_text(texte, reply_markup=reply_markup)

async def genere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Ex: /genere affiche pub pour Komara Agency")
        return

    await update.message.reply_text("Génération en cours... ⏳ 15s")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [{
            "parts": [{"text": f"Image réaliste 4k pour entreprise africaine: {prompt}"}]
        }],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"]
        }
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=60.0)

        response.raise_for_status()
        data = response.json()

        parts = data['candidates'][0]['content']['parts']
        image_data = None
        for part in parts:
            if 'inlineData' in part:
                image_data = part['inlineData']['data']
                break

        if not image_data:
            await update.message.reply_text("L'IA n'a pas pu générer d'image pour cette requête.")
            return

        image_bytes = base64.b64decode(image_data)
        photo = io.BytesIO(image_bytes)
        photo.name = "image.png"

        await update.message.reply_photo(photo, caption=f"Voici: {prompt}")

    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"Erreur API Gemini: {e.response.text}")
    except Exception as e:
        await update.message.reply_text(f"Erreur inattendue: {e}")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "Vos services":
        await update.message.reply_text("Je cherche les infos... ⏳")
        try:
            reponse = await ask_gemini_with_knowledge("Présente tous vos services avec leurs prix et descriptions.")
            await update.message.reply_text(reponse, reply_markup=reply_markup)
        except Exception as e:
            await update.message.reply_text(f"Erreur: {e}", reply_markup=reply_markup)

    elif text == "Tarifs":
        await update.message.reply_text(
            "Tarifs 🇬🇳\nSite: 150.000 GNF\nLogo: 100.000 GNF\nChatbot: 300.000 GNF/mois",
            reply_markup=reply_markup,
        )
    elif text == "Commander":
        await update.message.reply_text(
            "Décris-moi ton projet ou envoie /genere + ton idée",
            reply_markup=reply_markup,
        )
    elif text == "Contact":
        await update.message.reply_text(
            "Contact: WhatsApp +224 XXX",
            reply_markup=reply_markup,
        )
    elif text == "Portfolio":
        await update.message.reply_text(
            "Portfolio: https://komara-agency.onrender.com",
            reply_markup=reply_markup,
        )
    elif text == "Vidéo IA":
        await update.message.reply_text(
            "Envoie /genere + 'video 8s:...'",
            reply_markup=reply_markup,
        )
    else:
        # Tout autre message → question libre au bot avec la base de connaissances
        await update.message.reply_text("Je regarde ça... ⏳")
        try:
            reponse = await ask_gemini_with_knowledge(text)
            await update.message.reply_text(reponse, reply_markup=reply_markup)
        except Exception as e:
            await update.message.reply_text(f"Erreur: {e}", reply_markup=reply_markup)


def main():
    # Démarrer le serveur de health check dans un thread séparé
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("genere", genere))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    print("Bot KomaraBot lancé...")
    app.run_polling()

if __name__ == "__main__":
    main()
