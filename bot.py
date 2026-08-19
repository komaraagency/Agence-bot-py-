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
# ID Telegram du propriétaire (toi, Ndine). Seul cet ID peut modifier le bot.
# À définir dans les variables d'environnement Railway. Le bot se souvient
# de l'ID du premier /start automatiquement si OWNER_ID n'est pas défini.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
DEMANDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demandes_clients.json")

def save_demande(demande: dict):
    """Sauvegarde une demande client dans demandes_clients.json.
    Le propriétaire peut consulter ce fichier dans le repo pour voir toutes les demandes."""
    try:
        demandes = []
        if os.path.exists(DEMANDES_FILE):
            with open(DEMANDES_FILE, "r", encoding="utf-8") as f:
                demandes = json.load(f)
        demande["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        demandes.append(demande)
        with open(DEMANDES_FILE, "w", encoding="utf-8") as f:
            json.dump(demandes, f, ensure_ascii=False, indent=2)
        print(f"✅ Demande sauvegardée: {demande.get('service', '?')} - {demande.get('phone', '?')}")
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde demande: {e}")

async def notify_owner_order(context: ContextTypes.DEFAULT_TYPE, demande: dict):
    """Envoie une notification Telegram au propriétaire avec les détails de la commande."""
    if not OWNER_ID:
        return
    msg = (
        f"🔔 NOUVELLE COMMANDE\n\n"
        f"Service: {demande.get('service', 'N/A')}\n"
        f"Projet: {demande.get('details', 'N/A')}\n"
        f"Tel client: {demande.get('phone', 'N/A')}\n"
        f"Date: {demande.get('date', 'N/A')}\n"
        f"Client TG: {demande.get('telegram_name', 'N/A')} (ID: {demande.get('telegram_id', 'N/A')})"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=msg)
    except Exception as e:
        print(f"⚠️ Erreur notification propriétaire: {e}")

async def describe_photo_with_gemini(photo_bytes: bytes) -> str:
    """Décrit une photo en utilisant Gemini Vision (texte uniquement, pas de retouche).
    Réponse stable et cohérente pour le client."""
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")

    prompt = (
        "Analyse cette image en détail. Décris ce que tu vois en français de façon précise et naturelle:\n"
        "- Le sujet principal (personne, objet, scène)\n"
        "- Le cadrage et la composition\n"
        "- Les couleurs et la lumière\n"
        "- L'arrière-plan / décor\n"
        "- Le style et l'ambiance\n"
        "- Tout texte visible dans l'image (retranscris-le)\n\n"
        "Sois concis (5-8 lignes max), comme un photographe professionnel qui décrit une photo à un client. "
        "N'ajoute PAS de remarque sur ce qu'il faudrait améliorer, juste une description factuelle."
    )

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 500},
    }

    text, error = await _call_gemini_text(payload, timeout=30.0)
    if text:
        return text
    print(f"⚠️ Erreur description photo: {error}")
    return None

async def start_order_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Démarre le processus de commande guidée — le bot agit comme un vrai vendeur."""
    services = KNOWLEDGE.get("services", [])
    if not services:
        await update.message.reply_text(
            "Services temporairement indisponibles. Contactez-nous directement.",
            reply_markup=reply_markup,
        )
        return

    lignes = [f"{i+1}. {s['nom']} — {s['prix']}" for i, s in enumerate(services)]
    texte = (
        "Parfait, je vais vous accompagner dans votre commande! 🛒\n\n"
        "Quel service vous intéresse?\n\n"
        + "\n".join(lignes)
        + "\n\nRépondez avec le NUMÉRO du service (ex: 1).\n"
        "Tapez 'annuler' à tout moment pour annuler."
    )
    context.user_data["order_state"] = "service"
    context.user_data["order_data"] = {}
    await update.message.reply_text(texte, reply_markup=reply_markup)

async def handle_order_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Gère le flux de commande conversationnel étape par étape.
    Retourne True si le message a été traité (pour arrêter le routage normal)."""
    state = context.user_data.get("order_state")
    if not state:
        return False

    # Annulation à tout moment
    if text.lower() in ("annuler", "cancel", "annulation", "menu", "retour"):
        context.user_data.pop("order_state", None)
        context.user_data.pop("order_data", None)
        await update.message.reply_text(
            "Commande annulée. Que puis-je faire d'autre pour vous?",
            reply_markup=reply_markup,
        )
        return True

    data = context.user_data.get("order_data", {})
    services = KNOWLEDGE.get("services", [])

    if state == "service":
        chosen = None
        # Choix par numéro
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            if 0 <= idx < len(services):
                chosen = services[idx]
        # Choix par nom approximatif
        if chosen is None:
            lowered = text.lower()
            for s in services:
                if lowered in s["nom"].lower() or s["nom"].lower() in lowered:
                    chosen = s
                    break

        if chosen is None:
            await update.message.reply_text(
                "Je n'ai pas reconnu ce service. Répondez avec le NUMÉRO "
                "(ex: 1 pour " + services[0]["nom"] + ")",
                reply_markup=reply_markup,
            )
            return True

        data["service"] = chosen["nom"]
        data["prix"] = chosen["prix"]
        data["delai"] = chosen["delai"]
        context.user_data["order_state"] = "details"
        await safe_reply(update,
            f"Excellent choix! *{chosen['nom']}* ({chosen['prix']})\n\n"
            f"{chosen['description']}\n\n"
            "Décrivez votre projet en quelques mots:\n"
            "(ex: 'Je veux un site e-commerce pour vendre des vêtements')\n\n"
            "Tapez 'annuler' pour annuler."
        )
        return True

    elif state == "details":
        if len(text.strip()) < 5:
            await update.message.reply_text(
                "Décrivez un peu plus votre projet pour qu'on puisse bien vous aider. 🙂",
                reply_markup=reply_markup,
            )
            return True
        data["details"] = text.strip()
        context.user_data["order_state"] = "phone"
        await update.message.reply_text(
            "Parfait, j'ai noté votre projet. ✅\n\n"
            "Maintenant, quel est votre numéro de téléphone (WhatsApp de préférence)?\n"
            "Komara Agency vous contactera avec un devis personnalisé.\n\n"
            "Tapez 'annuler' pour annuler.",
            reply_markup=reply_markup,
        )
        return True

    elif state == "phone":
        phone = text.strip()
        # Validation basique: au moins 6 caractères, contient des chiffres
        digits = sum(c.isdigit() for c in phone)
        if digits < 6:
            await update.message.reply_text(
                "Ce numéro semble trop court. Réessayez avec un numéro valide\n"
                "(ex: +224 6XX XXX XXX ou +212 6XX XXX XXX).",
                reply_markup=reply_markup,
            )
            return True

        data["phone"] = phone
        data["telegram_id"] = update.effective_user.id if update.effective_user else 0
        data["telegram_name"] = update.effective_user.full_name if update.effective_user else "N/A"

        # Récapitulatif
        context.user_data["order_state"] = "confirm"
        recap = (
            "*Récapitulatif de votre commande*\n\n"
            f"Service: {data.get('service', 'N/A')}\n"
            f"Prix: {data.get('prix', 'N/A')}\n"
            f"Délai estimé: {data.get('delai', 'N/A')}\n"
            f"Votre projet: {data.get('details', 'N/A')}\n"
            f"Votre numéro: {data.get('phone', 'N/A')}\n\n"
            "Confirmez-vous cette commande?\n"
            "Tapez *oui* pour confirmer, *non* pour annuler."
        )
        await update.message.reply_text(recap, reply_markup=reply_markup, parse_mode="Markdown")
        return True

    elif state == "confirm":
        if text.lower().strip() in ("oui", "confirme", "confirmer", "ok", "yes", "o", "go"):
            # Sauvegarder la demande dans le fichier
            save_demande(data)
            # Notifier le propriétaire via Telegram
            await notify_owner_order(context, data)
            # Générer un lien WhatsApp pour le client
            wa_msg = (
                f"Bonjour Komara Agency, je confirme ma commande:\n"
                f"Service: {data.get('service', 'N/A')}\n"
                f"Projet: {data.get('details', 'N/A')}\n"
                f"Mon numéro: {data.get('phone', 'N/A')}"
            )
            wa_number = KNOWLEDGE['contact']['whatsapp'].replace('+', '').replace(' ', '')
            wa_link = f"https://wa.me/{wa_number}?text={quote(wa_msg)}"

            context.user_data.pop("order_state", None)
            context.user_data.pop("order_data", None)

            await update.message.reply_text(
                f"Commande confirmée! ✅\n\n"
                f"Komara Agency vous contactera très vite au {data.get('phone', 'N/A')}.\n\n"
                f"Pour accélérer, cliquez ici pour nous écrire directement:\n{wa_link}\n\n"
                f"{KNOWLEDGE['slogan']} 🚀",
                reply_markup=reply_markup,
            )
            return True
        elif text.lower().strip() in ("non", "annuler", "cancel", "n", "no"):
            context.user_data.pop("order_state", None)
            context.user_data.pop("order_data", None)
            await update.message.reply_text(
                "Commande annulée. Que puis-je faire d'autre pour vous?",
                reply_markup=reply_markup,
            )
            return True
        else:
            await update.message.reply_text(
                "Tapez *oui* pour confirmer ou *non* pour annuler.",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return True

    return False

# Libellés lisibles pour chaque fichier d'exemple (utilisés dans le menu /exemples).
# Fallback automatique sur le nom de fichier si un nouveau fichier n'est pas listé ici.
EXAMPLE_LABELS = {
    "portrait_studio_homme.jpg": "Portrait studio (bonnet noir)",
    "flyer_tarifs_komara.jpg": "Flyer tarifs",
    "portrait_tenue_traditionnelle.jpg": "Portrait tenue traditionnelle",
    "lifestyle_cuisine_couple.jpg": "Lifestyle cuisine",
    "pub_business_24_7.jpg": "Pub \"Business 24/7\"",
    "scene_the_marocain.jpg": "Scène thé marocain",
    "portrait_studio_bonnet_ka.jpg": "Portrait studio (logo KA)",
    "creatif_bouteille_ndine_komara.jpg": "Visuel créatif original",
}

# Script de génération et retouche d'image — guide le comportement du bot
IMAGE_GENIUS_PROMPT = """Tu es Komara Agency, bot génératif d'image et retouche photo professionnelle.

MISSION
Créer des images originales et retoucher des photos uploadées avec précision, fidélité et créativité. Objectif: visuels prêts pour réseaux sociaux, e-commerce, branding.

COMPÉTENCES
1. Génération: Transforme un prompt en image cohérente, esthétique et détaillée.
2. Retouche: Modifie l'image uploadée sans déformer l'identité. Change fond, lumière, couleur, vêtements, supprime/ajoute des éléments.
3. Restauration: Améliore photos floues, anciennes, abîmées en gardant le naturel.
4. Style: Applique photo réaliste, illustration, 3D, cinématique, minimaliste, aquarelle, anime.

RÈGLES DE GÉNÉRATION
- Suis le prompt avec précision. Si un détail est flou, garde un rendu réaliste et neutre.
- Respecte la composition: cadrage, perspective, lumière, ombres cohérentes.
- Visages et mains: naturels, sans déformation ni doigt en trop.
- Texte: écris-le lisiblement uniquement si demandé.
- Vise la meilleure qualité sans artefact ni flou excessif.

PRINCIPES DE RETOUCHE
1. Fidélité: Ne change que ce qui est demandé. Garde l'identité et l'expression.
2. Réalisme: Les retouches doivent être invisibles. Pas d'effet plastique.
3. Cohérence: Lumière, couleur, grain doivent matcher avec l'original.
4. Non-destructif: Préserve la qualité et les détails importants.

STYLES
Photo réaliste, portrait studio, lifestyle, produit e-commerce, illustration vectorielle, 3D, cinématique, noir et blanc, vintage.
Par défaut, utilise photo réaliste haute qualité.

INTERDITS
- Images sexuelles impliquant des mineurs. Refus immédiat.
- Deepfake trompeur, harcèlement, usurpation d'identité.
- Contenu violent, gore, haineux, illégal.
- Retouche non consentie du visage d'une personne réelle à des fins trompeuses.
Si la demande viole ces règles, refuse et propose une alternative sûre.

FLUX DE TRAVAIL
1. Analyse le prompt ou l'image uploadée.
2. Si info manquante, pose 1 question courte sur style, cadrage, couleur.
3. Exécute la génération ou retouche.
4. Propose 1-2 variantes: "Veux-tu plus lumineux?", "Autre fond?".

FORMAT DE RÉPONSE
Sois concis. Pour une retouche, décris en 1 ligne ce qui a changé.
Ex: "Fond changé en blanc studio, lumière adoucie, peau retouchée naturellement."
Termine par une proposition d'amélioration."""
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

import asyncio as _asyncio

THINKING_MESSAGES = [
    "réflexions...", "analyse...", "je réfléchis...", "un instant...",
    "je prépare ça...", "traitement...", "à l\'écoute...",
]

async def safe_reply(update: Update, text: str):
    """Affiche un indicateur 'réflexion' pendant 1-3s (chat action + message éphémère)
    avant d'envoyer la vraie réponse — donne l'impression d'une vraie réflexion humaine
    plutôt que d'un déclenchement instantané robotique."""
    import random as _r
    thinking_msg = _r.choice(THINKING_MESSAGES)

    # 1. Indicateur de frappe Telegram ("Bot is typing...")
    try:
        await update.get_bot().send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
    except Exception:
        pass

    # 2. Envoyer le message "réflexion..." éphémère
    thinking = None
    try:
        thinking = await update.message.reply_text(thinking_msg, reply_markup=None)
    except Exception:
        pass

    # 3. Attendre 1.5-3s pour simuler la réflexion (pas trop long, max 3s)
    delay = _r.uniform(1.5, 3.0)
    await _asyncio.sleep(delay)

    # 4. Supprimer le message "réflexion..."
    if thinking:
        try:
            await thinking.delete()
        except Exception:
            pass

    # 5. Envoyer la vraie réponse. Telegram (mode Markdown legacy) attend *gras*
    # en astérisque SIMPLE — mais Gemini écrit souvent **gras** (style CommonMark).
    # Ce mélange fait planter/tronquer le parsing des entités. On normalise donc
    # **texte** -> *texte* avant d'envoyer, avec repli en texte brut si ça échoue
    # quand même (ex: astérisques non appariés dans le texte du client).
    normalized_text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    try:
        await update.message.reply_text(normalized_text, reply_markup=reply_markup, parse_mode="Markdown")
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

# Modèles Gemini texte testés dans l'ordre — Google déprécie/renomme ses modèles
# régulièrement (ex: gemini-2.5-flash a été retiré, remplacé par gemini-3.6-flash).
# On essaie plusieurs candidats au lieu de dépendre d'un seul nom figé.
GEMINI_TEXT_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

async def _call_gemini_text(payload: dict, timeout: float = 30.0):
    """Appelle Gemini texte en essayant chaque modèle candidat jusqu'à ce qu'un
    fonctionne. Retourne (texte, None) ou (None, error_str) si tous échouent."""
    last_error = None
    for model in GEMINI_TEXT_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=timeout)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                print(f"⚠️ Modèle texte {model} a échoué: {last_error}")
                continue
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            # Les modèles Gemini "thinking" renvoient parfois des parts de
            # raisonnement interne (thought=true) EN PLUS de la vraie réponse.
            # On les ignore pour ne jamais exposer le raisonnement brut au client.
            text = "".join([p["text"] for p in parts if p.get("text") and not p.get("thought")]).strip()
            if text:
                return text, None
            last_error = f"Réponse 200 mais aucun texte: {data}"[:300]
        except Exception as e:
            # str(e) peut être vide pour certaines exceptions (ex: timeout) — on
            # ajoute toujours le type pour ne jamais logger une ligne vide.
            last_error = f"{type(e).__name__}: {str(e) or '(pas de message)'}"[:300]
            print(f"⚠️ Modèle texte {model} a échoué (exception): {last_error}")
    return None, last_error

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
        "1. Sois le meilleur commercial. Conseille et vends les services avec naturel.\n"
        "2. Réponds d'abord avec les infos de la BASE DE CONNAISSANCES.\n"
        "3. Si info manquante (hors démo image), dis: 'Je vous mets en contact avec un expert' et donne le WhatsApp.\n"
        "RÈGLES DE TON — TRÈS IMPORTANT:\n"
        "- Tu discutes avec un client qui te parle DÉJÀ, ce n'est pas un premier contact à chaque fois. "
        "NE COMMENCE JAMAIS ta réponse par 'Bonjour', une salutation, ou une question générique du "
        "type 'Comment puis-je vous aider ?' / 'Comment puis-je vous aider aujourd'hui ?' — c'est "
        "robotique et répétitif quand ça revient à chaque message. Réponds DIRECTEMENT à ce que le "
        "client vient de dire, comme le ferait un vrai commercial humain en pleine conversation.\n"
        "- Écris comme une vraie personne compétente qui discute, pas comme un script figé qui se "
        "présente ('En tant qu'assistant commercial...'). Varie tes formulations d'un message à l'autre.\n"
        "- Ton chaleureux, direct, professionnel mais décontracté — pas corporate ni ampoulé.\n"
        "- Français professionnel, vouvoiement\n"
        "- Réponse courte, structurée avec *gras* (UN SEUL astérisque de chaque côté, jamais deux) et listes •\n"
        "- 2 émojis max\n"
        "- Termine par un appel à l'action naturel: 'Voulez-vous un devis?' ou 'Contact: +212701986219'\n"
        "- Cite le slogan 1 fois sur 3, jamais en ouverture de message"
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\nQuestion du client: {question}"}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 1000},
    }

    text, error = await _call_gemini_text(payload, timeout=30.0)
    if text:
        return text
    print(f"⚠️ ask_gemini_with_knowledge: tous les modèles ont échoué — {error}")
    return f"Désolé, une erreur est survenue. Contactez-nous directement: {KNOWLEDGE['contact']['whatsapp']}"

def _is_owner(update: Update) -> bool:
    """Vérifie si l'utilisateur qui parle est le propriétaire du bot."""
    if not OWNER_ID:
        return True  # Si OWNER_ID non configuré, on laisse passer (dev/test)
    return update.effective_user and update.effective_user.id == OWNER_ID

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /admin — réservée au propriétaire. Affiche l'état et permet
    de gérer le bot. Aucun autre utilisateur ne peut modifier quoi que ce soit."""
    if not _is_owner(update):
        await update.message.reply_text(
            "Cette commande est réservée au propriétaire du bot. 🚫\n"
            "Pour utiliser le bot: /start, /genere, /exemples",
            reply_markup=reply_markup,
        )
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Panel admin — KomaraBot\n\n"
        f"Votre ID Telegram: {uid}\n"
        f"Statut: Bot actif ✅\n"
        f"Services: {len(KNOWLEDGE.get('services', []))} services\n"
        f"Exemples: {len(_list_example_files())} visuels\n\n"
        f"Pour configurer ce compte comme propriétaire, ajoutez dans les variables Railway:\n"
        f"OWNER_ID={uid}\n\n"
        f"Astuce: ajoutez le bot dans un groupe Telegram via le bouton \"Ajouter au groupe\" "
        f"depuis le profil du bot (@{context.bot.username}). N'importe qui peut l'ajouter, "
        f"mais seul vous pouvez le configurer.",
        reply_markup=reply_markup,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Première interaction: on retient l'ID du propriétaire si pas encore configuré
    global OWNER_ID
    if not OWNER_ID and update.effective_user:
        OWNER_ID = update.effective_user.id
        print(f"🔒 OWNER_ID auto-défini: {OWNER_ID} ({update.effective_user.full_name})")

    texte = f"""Salut! Je suis KomaraBot 🇬🇳
Assistant officiel de {KNOWLEDGE['agence']}
{KNOWLEDGE['slogan']}

Ce que je fais:
• Générer des images IA (/genere + description)
• Retoucher vos photos (envoyez une photo + vos instructions)
• Donner un devis pour site web/logo
• Présenter nos services

Commandes:
/genere + description → créer une image IA
/exemples → voir nos réalisations
Photo + texte → retoucher une photo
/admin → panel propriétaire

Ajoutez-moi dans un groupe Telegram pour que je sois accessible à tous!
(@KomaraBot > Ajouter au groupe)

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
    # Indicateur de réflexion + chat action
    try:
        await update.get_bot().send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    thinking = await update.message.reply_text("réflexion créative... 🎨", reply_markup=None)
    await __import__("asyncio").sleep(2)
    try:
        await thinking.delete()
    except Exception:
        pass
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

def _list_example_files():
    valid_exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        return sorted(
            f for f in os.listdir(EXAMPLES_DIR)
            if f.lower().endswith(valid_exts) and os.path.isfile(os.path.join(EXAMPLES_DIR, f))
        )
    except Exception:
        return []

def _label_for(filename: str) -> str:
    if filename in EXAMPLE_LABELS:
        return EXAMPLE_LABELS[filename]
    # Fallback: transforme le nom de fichier en libellé lisible
    name = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
    return name.capitalize()

async def exemples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche un MENU des exemples disponibles (texte uniquement, aucune photo
    envoyée à ce stade). Le client choisit un numéro -> le bot envoie UNE SEULE
    photo correspondante, pas plusieurs, pour ne pas spammer le chat."""
    image_files = _list_example_files()

    if not image_files:
        await update.message.reply_text(
            "Aucun exemple disponible pour le moment. 📸\n"
            "Nos réalisations arrivent bientôt !\n\n"
            f"En attendant, contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
        return

    # On mélange l'ordre affiché pour varier ce que les clients voient à chaque appel.
    random.shuffle(image_files)

    lignes = [f"{i+1}. {_label_for(f)}" for i, f in enumerate(image_files)]
    texte = (
        f"Voici nos modèles de réalisations {KNOWLEDGE['agence']} 📸\n\n"
        + "\n".join(lignes)
        + "\n\nRépondez avec le NUMÉRO du modèle qui vous intéresse pour le voir en photo."
    )

    # On mémorise la liste proposée pour cet utilisateur, dans l'ordre affiché,
    # afin de savoir quelle photo envoyer quand il répond par un numéro.
    context.user_data["pending_examples"] = image_files

    await update.message.reply_text(texte, reply_markup=reply_markup)

async def handle_example_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Si un menu /exemples est en attente pour cet utilisateur, tente de faire
    correspondre sa réponse (numéro ou nom) à UN SEUL exemple et l'envoie.
    Retourne True si la réponse a été traitée ici (pour arrêter le routage normal)."""
    pending = context.user_data.get("pending_examples")
    if not pending:
        return False

    choice = text.strip()
    selected = None

    # Correspondance par numéro (1-indexé)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(pending):
            selected = pending[idx]

    # Correspondance par nom/libellé approximatif
    if selected is None:
        lowered = choice.lower()
        for f in pending:
            if lowered in _label_for(f).lower() or lowered in f.lower():
                selected = f
                break

    if selected is None:
        # Réponse non reconnue: on redemande poliment, sans spammer de photo,
        # et on garde le menu actif pour un nouvel essai.
        await update.message.reply_text(
            "Je n'ai pas reconnu ce choix. Répondez avec le NUMÉRO du modèle "
            "affiché dans la liste ci-dessus (ex: 1).",
            reply_markup=reply_markup,
        )
        return True

    # On envoie EXACTEMENT une photo, puis on nettoie l'état pour ce client.
    context.user_data.pop("pending_examples", None)
    img_path = os.path.join(EXAMPLES_DIR, selected)
    try:
        with open(img_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"{_label_for(selected)} — Réalisé par {KNOWLEDGE['agence']} ✨",
                reply_markup=reply_markup,
            )
        await update.message.reply_text(
            "Voulez-vous un projet similaire? Tapez /genere + votre idée pour un visuel sur-mesure. 🎨",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur envoi exemple {selected}: {e}")
        await update.message.reply_text(
            f"Désolé, une erreur est survenue. Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
    return True

# Modèles Gemini Image testés dans l'ordre — les noms de modèles Google changent
# régulièrement (preview -> stable -> renommage), donc on essaie plusieurs candidats
# au lieu de dépendre d'un seul nom qui peut devenir invalide du jour au lendemain.
GEMINI_IMAGE_MODELS = [
    "gemini-2.5-flash-image-preview",
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-lite-image",
]

async def _call_gemini_image_model(model: str, edit_prompt: str, b64_image: str):
    """Appelle UN modèle Gemini Image donné. Retourne (image_bytes, description, error_str)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": edit_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=60.0)
        if response.status_code != 200:
            # On logue le VRAI message d'erreur renvoyé par Google (clé invalide,
            # modèle inexistant, quota à 0, etc.) au lieu de le cacher — indispensable
            # pour diagnostiquer précisément pourquoi Gemini échoue.
            return None, None, f"HTTP {response.status_code}: {response.text[:300]}"
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        image_bytes = None
        description = None
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                image_bytes = base64.b64decode(inline["data"])
            elif part.get("text") and not part.get("thought"):
                description = part["text"].strip()
        if image_bytes is None:
            return None, None, f"Réponse 200 mais aucune image dans les parts: {data}"[:300]
        return image_bytes, description, None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e) or '(pas de message)'}"[:300]

async def edit_photo_with_gemini_image(photo_bytes: bytes, user_instructions: str):
    """Retouche RÉELLE de la photo — contrairement à Pollinations (texte -> nouvelle image
    générée from scratch, donc incohérente avec l'original), Gemini Image (Nano Banana)
    édite DIRECTEMENT les pixels de la photo fournie. La pose, le décor, l'identité du
    sujet et le cadrage sont donc préservés — seuls les éléments demandés par le client
    changent. On force l'usage de Gemini: on essaie CHAQUE modèle candidat avant
    d'abandonner, aucun repli vers une génération texte->image non fiable.

    Retourne (image_bytes, description_texte) ou (None, None) si tous les modèles échouent."""
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")

    edit_prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Voici une photo à retoucher. Applique STRICTEMENT et UNIQUEMENT les instructions "
        "du client ci-dessous. Ne change RIEN d'autre: garde exactement la même pose, le même "
        "cadrage, la même composition, le même arrière-plan (sauf si demandé), la même identité "
        "du sujet. Aucun déplacement, repositionnement, ajout ou suppression non demandé. "
        "Aucun recadrage sauf si explicitement demandé.\n\n"
        f"Instructions du client: {user_instructions}\n\n"
        "Après l'image, ajoute UNE ligne en français décrivant ce qui a été changé."
    )

    for model in GEMINI_IMAGE_MODELS:
        image_bytes, description, error = await _call_gemini_image_model(model, edit_prompt, b64_image)
        if image_bytes:
            print(f"✅ Retouche réussie avec le modèle: {model}")
            return image_bytes, description
        print(f"⚠️ Modèle {model} a échoué: {error}")

    print("❌ TOUS les modèles Gemini Image ont échoué pour cette retouche.")
    return None, None

async def analyze_photo_with_gemini(photo_bytes: bytes, user_instructions: str) -> str:
    """Utilise Gemini Vision pour analyser la photo uploadée et produire un prompt
    de génération détaillé qui combine la description de l'image + les instructions
    du client. Le résultat alimente Pollinations.ai pour la génération finale."""
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")

    analyze_prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Tu vas recevoir une image uploadée par un client. Analyse-la en détail: "
        "sujet, cadrage, couleurs, lumière, arrière-plan, vêtements, style, ambiance. "
        "Ensuite, combine cette analyse avec les instructions du client pour produire "
        "UN SEUL prompt en anglais, extrêmement détaillé, qui servira à générer une "
        "nouvelle image via un modèle text-to-image.\n\n"
        "RÈGLES CRITIQUES:\n"
        "- Le prompt final doit décrire EXACTEMENT ce que le client veut voir\n"
        "- Si le client demande de changer le fond, décris le NOUVEAU fond\n"
        "- Si le client demande de changer les couleurs, décris les NOUVELLES couleurs\n"
        "- Garde l'identité du sujet (âge, genre, pose) sauf si le client demande de changer\n"
        "- Ajoute: 'high quality, professional, detailed, 4k, photorealistic'\n"
        "- Ne JAMAIS ajouter de style non demandé par le client\n\n"
        f"Instructions du client: {user_instructions if user_instructions else 'Améliorer cette photo avec un rendu professionnel'}\n\n"
        "Réponds UNIQUEMENT avec le prompt en anglais, sans introduction ni explication."
    )

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": analyze_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800},
    }

    text, error = await _call_gemini_text(payload, timeout=45.0)
    if text:
        return text
    print(f"⚠️ Erreur analyse Gemini Vision: {error}")
    return None

async def describe_retouche_result(user_instructions: str, photo_description: str) -> str:
    """Génère une courte description en français de ce qui a été fait, style:
    'Fond changé en blanc studio, lumière adoucie, peau retouchée naturellement.'"""
    prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Le client a demandé une retouche. Voici ses instructions et la description de la photo originale. "
        "Décris en UNE ligne en français ce qui a été changé, puis propose une amélioration.\n\n"
        f"Instructions: {user_instructions}\n"
        f"Photo analysée: {photo_description[:500]}\n\n"
        "Format: description courte + une question d'amélioration."
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200},
    }

    text, error = await _call_gemini_text(payload, timeout=30.0)
    if text:
        return text
    print(f"⚠️ Erreur description retouche: {error}")
    return "Retouche effectuée selon vos instructions."

async def _process_photo_retouche(update: Update, photo_bytes: bytes, user_instructions: str):
    """Factorise la logique de retouche photo — utilisée par handle_photo
    (photo + caption) et par handle_menu (choix 'retoucher' puis instructions)."""
    # Vérifier les interdits
    forbidden = ["mineur", "deepfake", "usurpation", "harcèlement", "violence", "gore"]
    if any(w in user_instructions.lower() for w in forbidden):
        await update.message.reply_text(
            "Je ne peux pas traiter cette demande. Elle va à l'encontre de mes règles d'utilisation. "
            "Je peux vous proposer une alternative sûre — décrivez votre besoin différemment. 🙏",
            reply_markup=reply_markup,
        )
        return

    # Indicateur de réflexion
    try:
        await update.get_bot().send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    _thinking = await update.message.reply_text("analyse de votre photo... 📸", reply_markup=None)
    await _asyncio.sleep(2)
    try:
        await _thinking.delete()
    except Exception:
        pass
    await update.message.reply_text("Analyse de votre photo et retouche en cours... ⏳ 20-30s", reply_markup=reply_markup)

    image_bytes, description = await edit_photo_with_gemini_image(photo_bytes, user_instructions)

    if image_bytes:
        result_photo = io.BytesIO(image_bytes)
        result_photo.name = "komara_retouche.png"
        caption = f"{description or 'Retouche effectuée selon vos instructions.'}\n\nVeux-tu une variante? Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}"
        await update.message.reply_photo(result_photo, caption=caption, reply_markup=reply_markup)
        return

    print("❌ Retouche impossible: tous les modèles Gemini Image ont échoué.")
    await update.message.reply_text(
        "Désolé, la retouche n'a pas pu être réalisée pour le moment (service IA "
        "temporairement indisponible). Réessayez dans 1-2 minutes, ou contactez "
        f"directement un expert: {KNOWLEDGE['contact']['whatsapp']}",
        reply_markup=reply_markup,
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les photos envoyées par le client — avec ou sans instructions en caption.
    Avec caption: analyse la photo + applique les instructions -> génère le résultat.
    Sans caption: demande ce que le client veut faire avec sa photo."""
    try:
        if not await _should_respond_in_group(update, context):
            return

        # Récupérer le texte (caption) s'il y en a un
        user_instructions = _strip_bot_mention(update.message.caption or "", context.bot.username)

        if not user_instructions:
            # Photo sans instructions: télécharger et stocker en mémoire, puis proposer 2 options
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())
            context.user_data["last_photo_bytes"] = photo_bytes
            context.user_data["photo_action"] = "awaiting_choice"
            await update.message.reply_text(
                "📸 Photo reçue! Que voulez-vous faire?\n\n"
                "1. Retoucher cette photo (fond, lumière, couleurs...)\n"
                "2. Analyser / décrire cette photo\n\n"
                "Répondez avec le NUMÉRO de votre choix (1 ou 2).",
                reply_markup=reply_markup,
            )
            return

        # Photo + instructions: retouche directe (factored function)
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())
        await _process_photo_retouche(update, photo_bytes, user_instructions)

    except httpx.HTTPStatusError as e:
        print(f"⚠️ Erreur HTTP génération: {e}")
        await update.message.reply_text(
            f"La génération a échoué. Réessayez avec une photo plus simple, "
            f"ou contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur handle_photo: {e}")
        await update.message.reply_text(
            f"Désolé, une erreur est survenue avec votre photo. 🙏 "
            f"Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )

def _strip_bot_mention(text: str, bot_username: str) -> str:
    if bot_username:
        text = text.replace(f"@{bot_username}", "").strip()
    return text

async def _should_respond_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """En groupe/canal, le bot ne répond QUE s'il est @mentionné ou si on répond
    directement à l'un de ses messages — sinon il reste silencieux pour ne pas
    polluer la conversation. En chat privé, il répond toujours."""
    chat_type = update.effective_chat.type
    if chat_type not in ("group", "supergroup"):
        return True

    bot_username = (context.bot.username or "").lower()
    text = (update.message.text or update.message.caption or "").lower()
    mentioned = bool(bot_username) and f"@{bot_username}" in text

    is_reply_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    return mentioned or is_reply_to_bot

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        if not await _should_respond_in_group(update, context):
            return
        text = _strip_bot_mention(text, context.bot.username)

        # 1. Si une commande est en cours, on gère le flux de commande en priorité
        # (le bot joue le rôle de vendeur: choix service -> description -> tel -> confirmation)
        if context.user_data.get("order_state"):
            if await handle_order_flow(update, context, text):
                return

        # 2. Si une action photo est en attente (choix analyse vs retouche)
        photo_action = context.user_data.get("photo_action")
        if photo_action == "awaiting_choice":
            choice = text.strip()
            if choice == "1":
                context.user_data["photo_action"] = "awaiting_retouche"
                await update.message.reply_text(
                    "Décrivez ce que vous voulez retoucher:\n"
                    "(ex: 'change le fond en blanc', 'améliore la lumière', 'rend en noir et blanc')\n\n"
                    "Tapez 'annuler' pour annuler.",
                    reply_markup=reply_markup,
                )
                return
            elif choice == "2":
                # Analyser / décrire la photo
                context.user_data.pop("photo_action", None)
                # Récupérer la dernière photo stockée
                last_photo = context.user_data.get("last_photo_bytes")
                if last_photo:
                    try:
                        await update.get_bot().send_chat_action(chat_id=update.effective_chat.id, action="typing")
                    except Exception:
                        pass
                    _thinking = await update.message.reply_text("analyse de votre photo... 📸", reply_markup=None)
                    await _asyncio.sleep(2)
                    try:
                        await _thinking.delete()
                    except Exception:
                        pass
                    description = await describe_photo_with_gemini(last_photo)
                    context.user_data.pop("last_photo_bytes", None)
                    if description:
                        await safe_reply(update, f"📸 *Voici mon analyse de votre photo:*\n\n{description}")
                    else:
                        await update.message.reply_text(
                            "Désolé, l'analyse a échoué pour le moment. Réessayez dans 1-2 minutes. 🙏",
                            reply_markup=reply_markup,
                        )
                else:
                    await update.message.reply_text(
                        "Je n'ai plus la photo en mémoire. Renvoyez-la s'il vous plaît. 📸",
                        reply_markup=reply_markup,
                    )
                return
            elif choice.lower() in ("annuler", "cancel", "menu"):
                context.user_data.pop("photo_action", None)
                context.user_data.pop("last_photo_bytes", None)
                await update.message.reply_text(
                    "Annulé. Que puis-je faire d'autre pour vous?",
                    reply_markup=reply_markup,
                )
                return
            else:
                await update.message.reply_text(
                    "Répondez avec 1 (retoucher) ou 2 (analyser).",
                    reply_markup=reply_markup,
                )
                return
        elif photo_action == "awaiting_retouche":
            if text.lower().strip() in ("annuler", "cancel", "menu"):
                context.user_data.pop("photo_action", None)
                context.user_data.pop("last_photo_bytes", None)
                await update.message.reply_text(
                    "Retouche annulée. Que puis-je faire d'autre pour vous?",
                    reply_markup=reply_markup,
                )
                return
            # Traiter comme instructions de retouche
            context.user_data.pop("photo_action", None)
            last_photo = context.user_data.get("last_photo_bytes")
            if last_photo:
                user_instructions = text.strip()
                await _process_photo_retouche(update, last_photo, user_instructions)
                context.user_data.pop("last_photo_bytes", None)
            else:
                await update.message.reply_text(
                    "Je n'ai plus la photo en mémoire. Renvoyez-la s'il vous plaît. 📸",
                    reply_markup=reply_markup,
                )
            return

        # 3. Si un menu /exemples est en attente pour ce client, on traite sa réponse
        # ici en priorité (un seul exemple envoyé), avant tout autre routage.
        if await handle_example_choice(update, context, text):
            return

        if text.lower() in ("exemple", "exemples", "/exemple", "/exemples"):
            await exemples(update, context)
            return

        if text == "Vos services":
            reponse = await ask_gemini_with_knowledge("Présente tous les services de Komara Agency avec prix et délais de façon commerciale.")
        elif text == "Tarifs":
            reponse = await ask_gemini_with_knowledge("Donne la liste des tarifs de tous les services.")
        elif text == "Commander":
            await start_order_flow(update, context)
            return
        elif text == "Contact":
            c = KNOWLEDGE['contact']
            reponse = f"📞 *Contactez {KNOWLEDGE['agence']}*\n\nWhatsApp: {c['whatsapp']}\nEmail: {c['email']}\nAdresse: {c['adresse']}\nPortfolio: {c['portfolio']}\n\n{KNOWLEDGE['slogan']}"
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
    app.add_handler(CommandHandler("exemple", exemples))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
