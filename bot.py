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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    print(f"✅ knowledge.json chargé pour {KNOWLEDGE.get('agence')}")
except Exception as e:
    print(f"⚠️ Erreur knowledge.json: {e}")
    KNOWLEDGE = None

menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

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
        "MISSION:\n"
        "1. Sois le meilleur commercial. Accueille, conseille, et vends les services.\n"
        "2. Réponds d'abord avec les infos de la BASE DE CONNAISSANCES.\n"
        "3. Si info manquante, dis: 'Je vous mets en contact avec un expert' et donne le WhatsApp.\n"
        "RÈGLES:\n"
        "- Français professionnel, vouvoiement\n"
        "- Réponse courte, structurée avec **gras** et listes •\n"
        "- 2 émojis max\n"
        "- Termine par un appel à l'action: 'Voulez-vous un devis?' ou 'Contact: +212701986219'\n"
        "- Cite le slogan 1 fois sur 3"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
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

Utilisez /genere + votre idée pour créer une image.
Ou tapez sur un bouton ci-dessous 👇"""
    await update.message.reply_text(texte, reply_markup=reply_markup)

async def genere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Ex: /genere affiche publicitaire pour Komara Agency")
        return
    await update.message.reply_text("Création de votre visuel IA en cours... ⏳ 20s")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"Image publicitaire 4k, style professionnel africain, pour entreprise: {prompt}"}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=60.0)
        response.raise_for_status()
        data = response.json()
        image_data = next((p['inlineData']['data'] for p in data['candidates'][0]['content']['parts'] if 'inlineData' in p), None)
        if not image_data:
            await update.message.reply_text("Impossible de générer. Essayez une description plus simple.")
            return
        photo = io.BytesIO(base64.b64decode(image_data))
        photo.name = "komara_image.png"
        await update.message.reply_photo(photo, caption=f"Voici votre visuel: {prompt}\n\nVoulez-vous l'utiliser pour une pub? Contactez-nous.")
    except Exception as e:
        await update.message.reply_text(f"Erreur de génération: {e}")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
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
    else:
        reponse = await ask_gemini_with_knowledge(text)
    await update.message.reply_text(reponse, reply_markup=reply_markup)

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("genere", genere))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    print(f"Bot {KNOWLEDGE['agence']} lancé...")
    app.run_polling()

if __name__ == "__main__":
    main()
