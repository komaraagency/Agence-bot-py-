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

# 1. LIRE LES CLÉS DEPUIS RAILWAY
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

# Menu clavier
menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

# --- Mini serveur HTTP pour le health check Railway ---
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

# --- Appel à Gemini (texte) avec la base de connaissances ---

async def ask_gemini_with_knowledge(question: str) -> str:
    knowledge_str = json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2) if KNOWLEDGE else "{}"

    system_prompt = (
        "Tu es KomaraBot, l'assistant virtuel officiel de Komara Agency 🇬🇳\n"
        "Komara Agency est une agence digitale basée à Conakry, Guinée. "
        "Slogan: Vision. Impact. Excellence.\n\n"

        "BASE DE CONNAISSANCES:\n"
        f"{knowledge_str}\n\n"

        "MISSION:\n"
        "1. Réponds de façon humaine, chaleureuse et professionnelle\n"
        "2. Utilise d'abord les infos de la BASE DE CONNAISSANCES\n"
        "3. Si l'info n'y est pas, dis-le et redirige vers WhatsApp: +212 701 986 219\n"

        "RÈGLES:\n"
        "- Réponds toujours en français, vouvoie le client\n"
        "- Sois concis: 3 à 5 phrases max\n"
        "- Utilise 1-2 émojis max et le gras **titre**\n"
        "- Propose une action: devis, contact, bouton du menu\n"
        "- Termine parfois par: Vision. Impact. Excellence."
    )

    # CORRECTION 1: Utiliser gemini-2.0-flash et pas flash-exp
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    # CORRECTION 2: Payload complet
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{system_prompt}\n\nQuestion du client: {question}"}]}
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 800,
        },
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return f"Erreur API Gemini: {e.response.status_code}. Vérifiez le nom du modèle et la clé API."
    except Exception as e:
        return f"Erreur de connexion à Gemini: {e}"

    parts = data["candidates"][0]["content"]["parts"]
    text_response = ""
    for part in parts:
        if "text" in part:
            text_response += part["text"]

    return text_response.strip() or "Désolé, je n'ai pas pu générer une réponse."

# --- Handlers Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte = """Salut! Je suis KomaraBot 🇬🇳
L'assistant officiel de Komara Agency.
Vision. Impact. Excellence.

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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

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
            await update.message.reply_text("L'IA n'a pas pu générer d'image. Essaie une description plus simple.")
            return

        image_bytes = base64.b64decode(image_data)
        photo = io.BytesIO(image_bytes)
        photo.name = "image.png"
        await update.message.reply_photo(photo, caption=f"Voici: {prompt}")

    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"Erreur API Gemini: {e.response.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Erreur inattendue: {e}")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "Vos services":
        await update.message.reply_text("Je cherche les infos... ⏳")
        reponse = await ask_gemini_with_knowledge("Présente tous vos services avec leurs prix et descriptions.")
        await update.message.reply_text(reponse, reply_markup=reply_markup)

    elif text == "Tarifs":
        await update.message.reply_text(
            "Tarifs Komara Agency 🇬🇳\n\n"
            "• Site Web: à partir de 1.500.000 GNF\n"
            "• Logo + Identité: à partir de 100.000 GNF\n"
            "• Chatbot IA: à partir de 300.000 GNF/mois\n"
            "• Community Management: à partir de 500.000 GNF/mois\n\n"
            "Contactez-nous pour un devis personnalisé 👇",
            reply_markup=reply_markup,
        )
    elif text == "Commander":
        await update.message.reply_text(
            "Parfait! Décris-moi ton projet ou envoie /genere + ton idée\n"
            "Ou contacte-nous directement: +212 701 986 219",
            reply_markup=reply_markup,
        )
    elif text == "Contact":
        await update.message.reply_text(
            "📞 Contact Komara Agency 🇬🇳\n\n"
            "WhatsApp: +212 701 986 219\n"
            "Localisation: Conakry, Guinée\n"
            "Portfolio: https://komara-agency.onrender.com\n"
            "Vision. Impact. Excellence.",
            reply_markup=reply_markup,
        )
    elif text == "Portfolio":
        await update.message.reply_text(
            "Découvre nos réalisations 👇\nhttps://komara-agency.onrender.com",
            reply_markup=reply_markup,
        )
    elif text == "Vidéo IA":
        await update.message.reply_text(
            "La génération vidéo n'est pas encore dispo sur Gemini Flash.\n"
            "Envoie /genere pour créer une image à la place.",
            reply_markup=reply_markup,
        )
    else:
        await update.message.reply_text("Je regarde ça... ⏳")
        reponse = await ask_gemini_with_knowledge(text)
        await update.message.reply_text(reponse, reply_markup=reply_markup)

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("genere", genere))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    print("Bot KomaraBot lancé...")
    app.run_polling()

if __name__ == "__main__":
    main()
