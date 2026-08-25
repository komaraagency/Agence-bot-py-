import os
import io
import re
import sys
import json
import base64
import threading
import random
from datetime import datetime
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
DEMANDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demandes_clients.json")

try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    SYSTEM_PROMPT = KNOWLEDGE.get("system_prompt", "Tu es Komara Agency.")
    print(f"✅ knowledge.json chargé pour {KNOWLEDGE.get('agence')}")
except Exception as e:
    sys.exit(f"ERREUR FATALE: knowledge.json invalide — {e}")

menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

# --- NOUVELLE FONCTION : GÉNÉRATION AVEC OU SANS PHOTO ---
async def generate_image_with_gemini(prompt: str, image_bytes: bytes = None) -> tuple:
    """Génère une image avec Gemini. Si image_bytes est fourni, il fait de l'img2img"""
    b64_image = None
    if image_bytes:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")

    # On injecte la VIBE KOMARA dans tous les prompts
    final_prompt = f"{SYSTEM_PROMPT}\n\nDEMANDE UTILISATEUR: {prompt}"

    parts = [{"text": final_prompt}]
    if b64_image:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_image}})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1024},
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image-preview:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if "inline_data" in part:
                        image_data = base64.b64decode(part["inline_data"]["data"])
                        return image_data, None
                    if "text" in part:
                        return None, part["text"]
    except Exception as e:
        return None, str(e)
    
    return None, "Aucune image générée"


# --- NOUVEAU : LE BOT COMPREND TOUT ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("order_state"):
        await handle_order_flow(update, context, update.message.text)
        return

    text = update.message.text
    photo = update.message.photo

    # Si c'est un bouton du menu
    if text in ["Vos services", "Tarifs", "Commander", "Portfolio", "Contact", "Vidéo IA"]:
        await handle_menu(update, context, text)
        return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking = await update.message.reply_text("🎨 Je crée votre visuel KOMARA...")

    image_bytes = None
    if photo:
        photo_file = await photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()

    # Si pas de texte mais juste une photo : on la décrit
    if not text and image_bytes:
        description = await describe_photo_with_gemini(image_bytes)
        await thinking.delete()
        await update.message.reply_text(f"J'ai analysé votre photo:\n\n{description}\n\nMaintenant dites-moi quoi changer: 'mets un fond bureau KOMARA'")
        return

    # GÉNÉRATION : Peu importe le format du prompt
    image_data, error = await generate_image_with_gemini(text, image_bytes)
    await thinking.delete()

    if image_data:
        await update.message.reply_photo(photo=io.BytesIO(image_data), caption=f"✨ Komara Agency\n{KNOWLEDGE['slogan']}")
    else:
        await update.message.reply_text(f"Erreur: {error}")


# Le reste de ton code : start, handle_menu, handle_order_flow, etc... reste identique
# Colle toutes tes autres fonctions ici en dessous

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bienvenue chez {KNOWLEDGE['agence']} 🇬🇳\n\n{KNOWLEDGE['description']}\n\nEnvoyez-moi juste votre idée et je la transforme en visuel premium. Ou envoyez une photo + dites ce que vous voulez changer.",
        reply_markup=reply_markup
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters
