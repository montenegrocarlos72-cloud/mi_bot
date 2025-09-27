import logging
from datetime import datetime
import random
import string
import gspread
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)

# ---------------- CONFIG ----------------
TOKEN = "TU_TOKEN_TELEGRAM"
ADMIN_IDS = [123456789, 987654321]  # IDs de admins
SPREADSHEET_ID = "TU_SHEET_ID"

# Google Sheets
gc = gspread.service_account(filename="credentials.json")
sh = gc.open_by_key(SPREADSHEET_ID)
worksheet = sh.sheet1

# Estados de conversación
NOMBRE, CEDULA, CODIGO, REFERIDO, ESPERAR_COMPROBANTE = range(5)

# Logger
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ---------------- HELPERS ----------------
def generar_codigo():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def save_user_to_sheet(user_id, name, cedula, codigo, referido_por=None):
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = [user_id, name, cedula, codigo, referido_por or "N/A", fecha]
    worksheet.append_row(data)

def get_referidos(user_id):
    records = worksheet.get_all_records()
    referidos = [r for r in records if str(r.get("ReferidoPor")) == str(user_id)]
    return referidos

# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Nueva Inversión", "Mis Referidos"], ["Soporte"]]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("👋 Bienvenido al bot de inversiones.\nElige una opción:", reply_markup=markup)

async def nueva_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✍️ Ingresa tu nombre completo:")
    return NOMBRE

async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text
    await update.message.reply_text("🆔 Ingresa tu número de cédula:")
    return CEDULA

async def recibir_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cedula"] = update.message.text
    codigo = generar_codigo()
    context.user_data["codigo"] = codigo
    await update.message.reply_text(f"🔑 Tu código de registro es: {codigo}\n\n¿Quién te refirió? (Escribe el ID o escribe 'ninguno')")
    return REFERIDO

async def recibir_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referido = update.message.text
    user = update.message.from_user

    nombre = context.user_data["nombre"]
    cedula = context.user_data["cedula"]
    codigo = context.user_data["codigo"]

    referido_val = referido if referido.lower() != "ninguno" else None

    save_user_to_sheet(user.id, nombre, cedula, codigo, referido_val)

    await update.message.reply_text("📤 Envía ahora tu comprobante de pago en formato de imagen.")
    return ESPERAR_COMPROBANTE

async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    photo = update.message.photo[-1].file_id

    await update.message.reply_text("✅ Hemos recibido tu comprobante. Será validado por un administrador.")

    for admin_id in ADMIN_IDS:
        await context.bot.send_photo(
            chat_id=admin_id,
            photo=photo,
            caption=f"📩 Nuevo comprobante de {user.first_name} (ID: {user.id})\n\n"
                    f"/aceptar {user.id} | /rechazar {user.id} [motivo]"
        )
    return ConversationHandler.END

async def aceptar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = context.args[0]
        await context.bot.send_message(chat_id=user_id, text="🎉 Tu comprobante fue aprobado. Bienvenido!")
        await update.message.reply_text(f"✅ Has aprobado el comprobante de {user_id}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def rechazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = context.args[0]
        motivo = " ".join(context.args[1:]) if len(context.args) > 1 else "Sin motivo"
        await context.bot.send_message(chat_id=user_id, text=f"❌ Tu comprobante fue rechazado.\nMotivo: {motivo}")
        await update.message.reply_text(f"❌ Has rechazado el comprobante de {user_id}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def mis_referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    referidos = get_referidos(user_id)

    if not referidos:
        await update.message.reply_text("📌 No tienes referidos registrados.")
        return

    msg = "👥 Tus referidos:\n"
    for r in referidos:
        msg += f"- {r['Nombre']} (Cédula: {r['Cedula']})\n"
    msg += f"\n💰 Pago estimado: {len(referidos) * 30000} COP"
    await update.message.reply_text(msg)

async def soporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📞 Para soporte comunícate con un administrador.")

# ---------------- MAIN ----------------
def main():
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Nueva Inversión$"), nueva_inversion)],
        states={
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)],
            CEDULA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cedula)],
            REFERIDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_referido)],
            ESPERAR_COMPROBANTE: [MessageHandler(filters.PHOTO, recibir_comprobante)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("aceptar", aceptar))
    application.add_handler(CommandHandler("rechazar", rechazar))
    application.add_handler(MessageHandler(filters.Regex("^Mis Referidos$"), mis_referidos))
    application.add_handler(MessageHandler(filters.Regex("^Soporte$"), soporte))

    application.run_polling()

if __name__ == "__main__":
    main()












