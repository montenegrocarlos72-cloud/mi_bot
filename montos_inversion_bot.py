import logging
import os
import random
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

# =============================
# CONFIGURACIONES
# =============================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",")]
FILE_ID_MONTOS = os.getenv("FILE_ID_MONTOS")      # Imagen de montos
FILE_ID_NX = os.getenv("FILE_ID_NX")              # Imagen de cuenta NX
SHEET_ID = os.getenv("SHEET_ID")                  # ID de la hoja de cálculo
GOOGLE_CREDS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # JSON (Railway secret)

# =============================
# LOGGING
# =============================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================
# GOOGLE SHEETS
# =============================
def get_sheet():
    creds = Credentials.from_service_account_info(eval(GOOGLE_CREDS), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh.sheet1  # primera hoja


def add_user_to_sheet(user_id, name, cedula, monto, referido, codigo, fecha_pago):
    sheet = get_sheet()
    data = [user_id, name, cedula, monto, referido, codigo, fecha_pago.strftime("%Y-%m-%d"), "Pendiente"]
    sheet.append_row(data)


def get_user_data(user_id):
    sheet = get_sheet()
    records = sheet.get_all_records()
    for row in records:
        if str(row["UserID"]) == str(user_id):
            return row
    return None


def get_user_referidos(codigo):
    sheet = get_sheet()
    records = sheet.get_all_records()
    referidos = [r for r in records if str(r.get("Referido", "")) == str(codigo)]
    return referidos

# =============================
# ESTADOS DE CONVERSACIÓN
# =============================
(
    ELEGIR_MONTO, CONFIRMAR_MONTO, REFERIDO, REGISTRO_NOMBRE,
    REGISTRO_CEDULA, ESPERAR_COMPROBANTE, MENU_OPCIONES
) = range(7)

# =============================
# BOTONES FIJOS
# =============================
def botones_fijos():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📞 Soporte"), KeyboardButton("🕒 Horarios")],
        [KeyboardButton("➕ Nueva inversión"), KeyboardButton("👥 Mis referidos")],
        [KeyboardButton("🚪 Salir")]
    ], resize_keyboard=True)

# =============================
# FLUJO DEL BOT
# =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(str(m), callback_data=f"monto_{m}")]
                 for m in range(200000, 501000, 50000)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_photo(FILE_ID_MONTOS, caption="💰 Selecciona el monto de tu inversión:", reply_markup=reply_markup)
    return ELEGIR_MONTO


async def elegir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    monto = int(query.data.split("_")[1])
    context.user_data["monto"] = monto
    pago = int(monto * 1.9)
    fecha_pago = datetime.now() + timedelta(days=10)
    context.user_data["fecha_pago"] = fecha_pago

    keyboard = [
        [InlineKeyboardButton("✅ Sí", callback_data="confirmar_si"),
         InlineKeyboardButton("❌ No", callback_data="confirmar_no")]
    ]
    await query.edit_message_caption(
        caption=f"Invertiste: {monto:,} COP\nRecibirás: {pago:,} COP\n📅 Fecha de pago: {fecha_pago.strftime('%Y-%m-%d')}\n\n¿Deseas continuar?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRMAR_MONTO


async def confirmar_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirmar_no":
        await query.edit_message_caption("❌ Gracias por tu interés. ¡Vuelve pronto con /start!")
        return ConversationHandler.END

    # Preguntar si tiene referido
    keyboard = [
        [InlineKeyboardButton("✅ Sí", callback_data="ref_si"),
         InlineKeyboardButton("❌ No", callback_data="ref_no")]
    ]
    await query.edit_message_caption("¿Vienes referido por alguien?", reply_markup=InlineKeyboardMarkup(keyboard))
    return REFERIDO


async def referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "ref_si":
        await query.edit_message_caption("Ingresa el código de referido (4 dígitos):")
        return REFERIDO

    if query.data == "ref_no":
        context.user_data["referido"] = "N/A"
        await query.edit_message_caption("📋 Vamos a registrarte. Por favor, escribe tu nombre completo:")
        return REGISTRO_NOMBRE


async def registro_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text
    await update.message.reply_text("Por favor, escribe tu número de cédula:")
    return REGISTRO_CEDULA


async def registro_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cedula = update.message.text
    context.user_data["cedula"] = cedula
    codigo = random.randint(1000, 9999)
    context.user_data["codigo"] = codigo

    # Guardar en Google Sheets
    add_user_to_sheet(
        user_id=update.message.from_user.id,
        name=context.user_data["nombre"],
        cedula=cedula,
        monto=context.user_data["monto"],
        referido=context.user_data.get("referido", "N/A"),
        codigo=codigo,
        fecha_pago=context.user_data["fecha_pago"]
    )

    await update.message.reply_photo(
        FILE_ID_NX,
        caption=f"✅ Registro exitoso.\nTu código: {codigo}\n\nPor favor, realiza el pago y envía el comprobante aquí."
    )
    return ESPERAR_COMPROBANTE


async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Por favor, envía una imagen del comprobante.")
        return ESPERAR_COMPROBANTE

    # Enviar a admins
    for admin_id in ADMIN_IDS:
        await context.bot.send_photo(
            chat_id=admin_id,
            photo=update.message.photo[-1].file_id,
            caption=f"Nuevo comprobante de {context.user_data['nombre']} (ID {update.message.from_user.id}).\nMonto: {context.user_data['monto']}"
        )

    await update.message.reply_text("📌 Tu comprobante será validado en 5-10 minutos. Gracias por tu paciencia.", reply_markup=botones_fijos())
    return MENU_OPCIONES

# =============================
# BOTONES FIJOS
# =============================
async def soporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📞 Soporte:\nWhatsApp1: +57 3000000000\nWhatsApp2: +57 3100000000\nEmail: vortex440@gmail.com")


async def horarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕒 Horarios:\nLunes a Sábado: 8am - 7pm\nDomingos: 8am - 12pm")


async def nueva_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def mis_referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = get_user_data(update.message.from_user.id)
    if not user_data:
        await update.message.reply_text("❌ No estás registrado aún.")
        return

    codigo = user_data["Codigo"]
    referidos = get_user_referidos(codigo)
    if not referidos:
        await update.message.reply_text("👥 Aún no tienes referidos.")
    else:
        msg = "\n".join([f"- {r['Nombre']} ({r['Cedula']})" for r in referidos])
        await update.message.reply_text(f"👥 Tus referidos:\n{msg}\n\n💰 Recuerda que ganas 30,000 COP por cada uno.")


async def salir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🙏 Gracias por confiar en nosotros. ¡Te esperamos pronto!", reply_markup=None)
    return ConversationHandler.END

# =============================
# MAIN
# =============================
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ELEGIR_MONTO: [CallbackQueryHandler(elegir_monto, pattern="^monto_")],
            CONFIRMAR_MONTO: [CallbackQueryHandler(confirmar_monto, pattern="^confirmar_")],
            REFERIDO: [
                CallbackQueryHandler(referido, pattern="^ref_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, registro_nombre)
            ],
            REGISTRO_NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, registro_nombre)],
            REGISTRO_CEDULA: [MessageHandler(filters.TEXT & ~filters.COMMAND, registro_cedula)],
            ESPERAR_COMPROBANTE: [MessageHandler(filters.PHOTO, recibir_comprobante)],
            MENU_OPCIONES: [
                MessageHandler(filters.Regex("(?i)soporte"), soporte),
                MessageHandler(filters.Regex("(?i)horarios"), horarios),
                MessageHandler(filters.Regex("(?i)nueva inversión"), nueva_inversion),
                MessageHandler(filters.Regex("(?i)mis referidos"), mis_referidos),
                MessageHandler(filters.Regex("(?i)salir"), salir),
            ]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()






