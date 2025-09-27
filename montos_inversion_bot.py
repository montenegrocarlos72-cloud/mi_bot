import os
import json
import random
import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
import gspread
from google.oauth2.service_account import Credentials

# ===========================
#   GOOGLE SHEETS SETUP
# ===========================
creds_json = os.getenv("GOOGLE_CREDS")
creds_dict = json.loads(creds_json)
creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)
SHEET = gc.open_by_key(os.getenv("SHEET_ID")).sheet1

# ===========================
#   VARIABLES
# ===========================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS").split(",")]
FILE_ID_MONTOS = os.getenv("FILE_ID_MONTOS")
FILE_ID_NX = os.getenv("FILE_ID_NX")

(
    MONTO, CONFIRMAR_INVERSION, REFERIDO, CONFIRMAR_REGISTRO,
    NOMBRE, CEDULA, ESPERAR_COMPROBANTE, ADMIN_BROADCAST
) = range(8)

MAIN_MENU = ReplyKeyboardMarkup(
    [["Nueva inversión", "Mis referidos"],
     ["Soporte", "Horarios"],
     ["Salir"]],
    resize_keyboard=True
)

# ===========================
#   FUNCIONES AUXILIARES
# ===========================
def generar_codigo():
    return str(random.randint(1000, 9999))

def calcular_pago(monto):
    return int(monto * 1.9)

def fecha_pago():
    return (datetime.date.today() + datetime.timedelta(days=10)).strftime("%d/%m/%Y")

def registrar_usuario(user_id, nombre, cedula, referido, codigo):
    SHEET.append_row([str(user_id), nombre, cedula, referido, codigo, str(datetime.date.today())])

def registrar_inversion(user_id, monto, codigo):
    SHEET.append_row([str(user_id), "INVERSION", monto, codigo, str(datetime.date.today()), fecha_pago()])

def obtener_referidos(codigo):
    data = SHEET.get_all_records()
    return [row for row in data if row.get("Referido") == codigo]

# ===========================
#   HANDLERS
# ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(str(x), callback_data=f"monto_{x}")]
                for x in range(200000, 501000, 50000)]
    await update.message.reply_photo(
        FILE_ID_MONTOS,
        caption="Bienvenido 🙌\nSelecciona el monto de inversión:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MONTO

async def elegir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    monto = int(query.data.split("_")[1])
    context.user_data["monto"] = monto
    pago = calcular_pago(monto)
    await query.edit_message_text(
        f"Elegiste invertir {monto:,}.\n"
        f"Recibirás {pago:,} en {fecha_pago()}.\n¿Confirmas?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Sí ✅", callback_data="confirmar_si")],
            [InlineKeyboardButton("No ❌", callback_data="confirmar_no")]
        ])
    )
    return CONFIRMAR_INVERSION

async def confirmar_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirmar_no":
        await query.edit_message_text("Gracias por visitarnos 🙏 Vuelve pronto.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    await query.edit_message_text(
        "¿Vienes referido por alguien?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Sí", callback_data="ref_si")],
            [InlineKeyboardButton("No", callback_data="ref_no")]
        ])
    )
    return REFERIDO

async def referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "ref_si":
        await query.edit_message_text("Ingresa el código de referido:")
        context.user_data["esperando_referido"] = True
        return REFERIDO
    else:
        await query.edit_message_text("¿Deseas registrarte?",
                                      reply_markup=InlineKeyboardMarkup([
                                          [InlineKeyboardButton("Sí ✅", callback_data="reg_si")],
                                          [InlineKeyboardButton("No ❌", callback_data="reg_no")]
                                      ]))
        return CONFIRMAR_REGISTRO

async def procesar_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("esperando_referido"):
        codigo = update.message.text.strip()
        context.user_data["referido"] = codigo
        await update.message.reply_text("¿Deseas registrarte?",
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("Sí ✅", callback_data="reg_si")],
                                            [InlineKeyboardButton("No ❌", callback_data="reg_no")]
                                        ]))
        return CONFIRMAR_REGISTRO

async def confirmar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "reg_no":
        await query.edit_message_text("Gracias por visitarnos 🙏 Vuelve pronto.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    await query.edit_message_text("Ingresa tu nombre completo:")
    return NOMBRE

async def guardar_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text.strip()
    await update.message.reply_text("Ingresa tu número de cédula:")
    return CEDULA

async def guardar_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cedula"] = update.message.text.strip()
    codigo = generar_codigo()
    context.user_data["codigo"] = codigo
    registrar_usuario(
        update.effective_user.id,
        context.user_data["nombre"],
        context.user_data["cedula"],
        context.user_data.get("referido", "N/A"),
        codigo
    )

    await update.message.reply_photo(
        FILE_ID_NX,
        caption=f"✅ Registro exitoso.\nTu código es: {codigo}\n\n"
                "Consigna y envía tu comprobante aquí.",
        reply_markup=MAIN_MENU
    )
    return ESPERAR_COMPROBANTE

async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        monto = context.user_data.get("monto", 0)
        codigo = context.user_data.get("codigo")
        registrar_inversion(update.effective_user.id, monto, codigo)

        for admin_id in ADMIN_IDS:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=file_id,
                caption=f"Nuevo comprobante de {context.user_data['nombre']} (Cédula: {context.user_data['cedula']}).\n"
                        f"Monto: {monto:,}\nCódigo: {codigo}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Aceptar ✅", callback_data=f"aceptar_{update.effective_user.id}")],
                    [InlineKeyboardButton("Rechazar ❌", callback_data=f"rechazar_{update.effective_user.id}")],
                    [InlineKeyboardButton("Enviar mensaje ✉️", callback_data=f"msg_{update.effective_user.id}")]
                ])
            )

        await update.message.reply_text("📌 Tu comprobante fue enviado a validación.\n"
                                        "Tendrá respuesta en 5-10 minutos.",
                                        reply_markup=MAIN_MENU)
        return ConversationHandler.END

# ===========================
#   ADMIN FUNCIONES
# ===========================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, user_id = query.data.split("_")
    user_id = int(user_id)

    if action == "aceptar":
        await context.bot.send_message(chat_id=user_id, text="✅ Tu comprobante fue validado. Gracias por confiar.", reply_markup=MAIN_MENU)
        await query.edit_message_caption(caption="Comprobante validado ✅")
    elif action == "rechazar":
        await context.bot.send_message(chat_id=user_id, text="❌ Tu comprobante fue rechazado. Vuelve a intentarlo.", reply_markup=MAIN_MENU)
        await query.edit_message_caption(caption="Comprobante rechazado ❌")
    elif action == "msg":
        context.user_data["msg_target"] = user_id
        await query.message.reply_text("✉️ Escribe el mensaje que deseas enviar al usuario:")
        return ADMIN_BROADCAST

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.user_data.get("msg_target")
    if target:
        await context.bot.send_message(chat_id=target, text=f"📩 Mensaje del administrador:\n\n{update.message.text}")
        await update.message.reply_text("✅ Mensaje enviado al usuario.")
    return ConversationHandler.END

# ===========================
#   MAIN
# ===========================
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MONTO: [CallbackQueryHandler(elegir_monto, pattern="^monto_")],
            CONFIRMAR_INVERSION: [CallbackQueryHandler(confirmar_inversion, pattern="^confirmar_")],
            REFERIDO: [
                CallbackQueryHandler(referido, pattern="^ref_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_referido)
            ],
            CONFIRMAR_REGISTRO: [CallbackQueryHandler(confirmar_registro, pattern="^reg_")],
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_nombre)],
            CEDULA: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_cedula)],
            ESPERAR_COMPROBANTE: [MessageHandler(filters.PHOTO, recibir_comprobante)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast)],
        },
        fallbacks=[]
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(aceptar|rechazar|msg)_"))
    app.run_polling()

if __name__ == "__main__":
    main()













