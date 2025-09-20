# montos_inversion_bot.py
import logging
import asyncio
import random
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import pandas as pd
import gspread
from google.oauth2 import service_account
import os, json


from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ---------------- CONFIG ----------------
# Leer sensible desde variables de entorno
TOKEN = os.getenv("TELEGRAM_TOKEN")  # debe estar en Railway / entorno
ADMIN_IDS = os.getenv("ADMIN_IDS", "")  # "8214551774,1592839102"
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]

FILE_ID_MONTOS = os.getenv("FILE_ID_MONTOS", "")  # opcional
FILE_ID_NEQUI = os.getenv("FILE_ID_NEQUI", "")    # opcional

SHEET_ID = os.getenv("SHEET_ID")  # ID de Google Sheet
# ---------------- STATES ----------------
(
    MONTO, CONFIRMAR_INVERSION, CODIGO_REFERIDO, CONFIRMAR_REGISTRO,
    NOMBRE, CEDULA, CONFIRMAR_DATOS, ESPERAR_COMPROBANTE, MENU_OPCIONES,
    ADMIN_BROADCAST_GET_MEDIA, ADMIN_BROADCAST_CONFIRM, ADMIN_REJECTION_REASON,
    NUEVA_INVERSION_MONTO, NUEVA_INVERSION_COMPROBANTE
) = range(15)

# Columnas estándar
STANDARD_COLUMNS = [
    "RecordID", "ChatID", "Nombre", "Cédula", "Monto", "Referido", "CodigoUsuario",
    "FechaRegistro", "FechaPago", "ComprobanteFileID", "Estado", "AdminComentario",
    # columnas para nuevas inversiones se agregarán dinámicamente: "NuevaInversion 1", "FechaPagoNueva 1", ...
]

# Logs
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Pending maps
pending_rejects: Dict[int, int] = {}    # admin_id -> user_chat_id (esperando motivo)
pending_checks: Dict[int, float] = {}   # user_chat_id -> timestamp

# ---------------- CREDENCIALES GOOGLE SHEETS ----------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Lee el JSON de la variable de entorno en Railway
service_account_info = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])

# Crea las credenciales
creds = service_account.Credentials.from_service_account_info(
    service_account_info, scopes=SCOPES
)

# Conecta gspread usando esas credenciales
client = gspread.authorize(creds)


def read_user_df() -> pd.DataFrame:
    """
    Lee la hoja de Google Sheets y devuelve un DataFrame con columnas STANDARD_COLUMNS.
    Si la hoja está vacía crea el encabezado.
    """
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SHEET_ID)
        ws = sh.sheet1
        records = ws.get_all_records()
        if not records:
            df = pd.DataFrame(columns=STANDARD_COLUMNS)
        else:
            df = pd.DataFrame(records)
            # Asegurar que tengamos las columnas estándar
            for col in STANDARD_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            # mantener orden
            df = df[[c for c in df.columns]]  # keep existing order
    except Exception as e:
        logger.error("Error leyendo Google Sheet: %s", e)
        # fallback a DataFrame vacío con columnas estándar
        df = pd.DataFrame(columns=STANDARD_COLUMNS)
    return df

def save_user_df(df: pd.DataFrame):
    """
    Sobrescribe la hoja con el contenido de df.
    """
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SHEET_ID)
        ws = sh.sheet1
        # Si df está vacío, aseguramos encabezados
        if df.empty:
            headers = STANDARD_COLUMNS
            ws.clear()
            ws.append_row(headers)
            return
        # Reemplazar todo el contenido
        headers = df.columns.tolist()
        values = df.values.tolist()
        ws.clear()
        ws.append_row(headers)
        if values:
            # gspread requiere listas; append_rows puede ser usada
            ws.append_rows(values)
    except Exception as e:
        logger.error("Error guardando Google Sheet: %s", e)

def generate_unique_code(df: pd.DataFrame) -> str:
    """Genera un código de 4 dígitos no existente en df['CodigoUsuario']"""
    existing = set(df["CodigoUsuario"].astype(str).tolist())
    while True:
        code = f"{random.randint(1000, 9999)}"
        if code not in existing and code != "":
            return code

# ---------------- Helper: teclado principal ----------------
def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["Mis referidos", "Soporte"], ["Horarios de atención", "Nueva inversión"], ["Salir"]],
        resize_keyboard=True, one_time_keyboard=False
    )

# ---------------- FUNCIONES BOT ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("Usuario %s inició /start", chat_id)

    # Si el usuario ya tiene interacciones, mostrar menú principal con opción nueva inversión
    df = read_user_df()
    has_user = False
    if not df.empty:
        mask = (df["ChatID"] == chat_id)
        if mask.any():
            has_user = True

    # Enviar imagen de montos
    amounts = [["200.000", "250.000"], ["300.000", "350.000"], ["400.000", "450.000"], ["500.000"]]
    keyboard = ReplyKeyboardMarkup(amounts, one_time_keyboard=True, resize_keyboard=True)
    try:
        if FILE_ID_MONTOS:
            await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_MONTOS)
    except Exception as e:
        logger.error("Error enviando imagen montos: %s", e)

    if not has_user:
        await update.message.reply_text(
            "💰 *Montos de inversión disponibles*\n\n"
            "Elige uno de los montos o escribe otro (usa puntos):",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return MONTO
    else:
        # Usuario conocido: mostrar menú principal (incluyendo Nueva inversión)
        await update.message.reply_text(
            "Bienvenido de nuevo. ¿Qué deseas hacer?",
            reply_markup=main_menu_keyboard()
        )
        return MENU_OPCIONES

async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        monto = int(text.replace(".", "").strip())
    except ValueError:
        await update.message.reply_text("❌ Ingresa un monto válido con puntos (ej: 200.000).")
        return MONTO

    if 200000 <= monto <= 500000:
        context.user_data["monto"] = monto
        ganancia = int(monto * 1.9)  # +90% => 190% = x1.9
        await update.message.reply_text(
            f"✅ Deseas invertir *{monto:,}* COP?\nEn 10 días recibirás *{ganancia:,}* COP.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Sí")], [KeyboardButton("No")]], one_time_keyboard=True, resize_keyboard=True)
        )
        return CONFIRMAR_INVERSION
    else:
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.")
        return MONTO

async def confirmar_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        # Pregunta sobre referido
        keyboard = ReplyKeyboardMarkup([[KeyboardButton("Sí")], [KeyboardButton("No")]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text("🔑 ¿Vienes referido por alguien? (Si / No)", reply_markup=keyboard)
        return CODIGO_REFERIDO
    else:
        await update.message.reply_text("❌ Gracias por ingresar, vuelve pronto.")
        return ConversationHandler.END

async def recibir_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codigo = update.message.text.strip()
    df = read_user_df()

    if codigo.lower() in ("no", "n"):
        context.user_data["referido"] = "Ninguno"
    else:
        # buscar por CodigoUsuario en la hoja
        if not df.empty and "CodigoUsuario" in df.columns:
            mask = df["CodigoUsuario"].astype(str) == codigo
            if mask.any():
                nombre_ref = df[mask]["Nombre"].values[-1]
                await update.message.reply_text(f"✅ Vienes referido por {nombre_ref}.")
                context.user_data["referido"] = codigo
            else:
                await update.message.reply_text("⚠️ Código no válido. Ingresa otro o escribe NO.")
                return CODIGO_REFERIDO
        else:
            await update.message.reply_text("⚠️ Aún no hay códigos en la base. Si tienes un código, espera que un admin lo registre.")
            return CODIGO_REFERIDO

    keyboard = ReplyKeyboardMarkup([[KeyboardButton("Sí")], [KeyboardButton("No")]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("📝 ¿Deseas continuar con el registro?", reply_markup=keyboard)
    return CONFIRMAR_REGISTRO

async def confirmar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        await update.message.reply_text("✍️ Escribe tu *nombre completo*:", parse_mode="Markdown")
        return NOMBRE
    else:
        await update.message.reply_text("❌ Gracias por ingresar, vuelve cuando estés seguro.")
        return ConversationHandler.END

async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text.strip()
    await update.message.reply_text("🆔 Ahora escribe tu *número de cédula*:", parse_mode="Markdown")
    return CEDULA

async def recibir_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cedula"] = update.message.text.strip()
    nombre = context.user_data["nombre"]
    cedula = context.user_data["cedula"]

    await update.message.reply_text(
        f"✅ Confirma tus datos:\n\n"
        f"👤 Nombre: {nombre}\n"
        f"🆔 Cédula: {cedula}\n\n"
        "¿Son correctos?",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Sí")], [KeyboardButton("No")]], one_time_keyboard=True, resize_keyboard=True)
    )
    return CONFIRMAR_DATOS

async def confirmar_datos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        nombre = context.user_data.get("nombre", "")
        cedula = context.user_data.get("cedula", "")
        referido = context.user_data.get("referido", "Ninguno")
        monto = context.user_data.get("monto", 0)
        chat_id = update.effective_chat.id

        df = read_user_df()
        ahora = datetime.now()
        fecha_pago = ahora + timedelta(days=10)

        # Crear nueva fila sin CodigoUsuario (se asigna al aprobar por admin)
        nuevo = {
            "RecordID": int(datetime.now().timestamp()),
            "ChatID": chat_id,
            "Nombre": nombre,
            "Cédula": cedula,
            "Monto": monto,
            "Referido": referido,
            "CodigoUsuario": "",
            "FechaRegistro": ahora.strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago.strftime("%Y-%m-%d"),
            "ComprobanteFileID": "",
            "Estado": "Esperando comprobante",
            "AdminComentario": ""
        }
        # Concatenar
        df = pd.concat([df, pd.DataFrame([nuevo], columns=list(nuevo.keys()))], ignore_index=True)
        # Aseguramos columnas estándar
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        save_user_df(df)

        # Mensaje y enviar cuenta nequi
        await update.message.reply_text(
            f"🎉 Registro inicial exitoso {nombre}!\n\n"
            f"Envía el comprobante de tu pago a la siguiente cuenta y luego espera la validación.\n\n"
            f"Fecha estimada de pago: *{fecha_pago.strftime('%Y-%m-%d')}* (10 días desde hoy).",
            parse_mode="Markdown"
        )
        try:
            if FILE_ID_NEQUI:
                await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_NEQUI)
        except Exception as e:
            logger.error("Error enviando imagen de cuenta: %s", e)

        context.user_data["chat_saved"] = True
        pending_checks[chat_id] = datetime.now().timestamp()
        return ESPERAR_COMPROBANTE
    else:
        await update.message.reply_text("❌ Corrige tus datos. Escribe tu nombre completo:")
        return NOMBRE

# ---------------- Recibir comprobante (registro o nueva inversión) ----------------
async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"

    if not file_id:
        await update.message.reply_text("⚠️ Envía una imagen o documento como comprobante.")
        return ESPERAR_COMPROBANTE

    chat_id = update.effective_chat.id
    df = read_user_df()

    # Buscamos la última fila con estado 'Esperando comprobante' o usuario por chat
    mask = (df["ChatID"] == chat_id) & (df["Estado"].str.contains("Esperando", na=False))
    if not mask.any():
        # si no se encuentra fila con Esperando, intentar marcar la última fila del usuario
        mask = (df["ChatID"] == chat_id)
    if mask.any():
        idx = df[mask].index[-1]
        df.at[idx, "ComprobanteFileID"] = file_id
        df.at[idx, "Estado"] = "Comprobante enviado"
        save_user_df(df)
    else:
        # Si no hay registro previo, creamos fila mínima
        nuevo = {
            "RecordID": int(datetime.now().timestamp()),
            "ChatID": chat_id,
            "Nombre": context.user_data.get("nombre", ""),
            "Cédula": context.user_data.get("cedula", ""),
            "Monto": context.user_data.get("monto", 0),
            "Referido": context.user_data.get("referido", "Ninguno"),
            "CodigoUsuario": "",
            "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "ComprobanteFileID": file_id,
            "Estado": "Comprobante enviado",
            "AdminComentario": ""
        }
        df = pd.concat([df, pd.DataFrame([nuevo], columns=list(nuevo.keys()))], ignore_index=True)
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        save_user_df(df)

    # Preparar caption para admins
    nombre = context.user_data.get("nombre", "(no registrado)")
    cedula = context.user_data.get("cedula", "(no registrado)")
    monto = context.user_data.get("monto", 0)
    caption = (
        f"📩 *Nuevo comprobante recibido*\n\n"
        f"👤 {nombre}\n"
        f"🆔 {cedula}\n"
        f"💰 {monto:,} COP\n\n"
        f"ChatID: `{chat_id}`"
    )

    # Inline keyboard para admins: Aprobar / Rechazar
    keyboard = [
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{chat_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{chat_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    # Enviar a admins
    for admin_id in ADMIN_IDS:
        try:
            if file_type == "photo":
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
            else:
                await context.bot.send_document(chat_id=admin_id, document=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.error("Error enviando comprobante a admin %s: %s", admin_id, e)

    await update.message.reply_text("⏳ Espera de 5 a 10 minutos mientras validamos tu transacción.")
    pending_checks[chat_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Recordatorio si admin no responde ----------------
async def check_pending_after_delay(context: ContextTypes.DEFAULT_TYPE, user_chat_id: int, delay_seconds: int = 600):
    await asyncio.sleep(delay_seconds)
    df = read_user_df()
    mask = (df["ChatID"] == user_chat_id) & (df["CodigoUsuario"] == "") & (df["Estado"].str.contains("Comprobante|Esperando", na=False))
    if mask.any():
        try:
            await context.bot.send_message(chat_id=user_chat_id, text="¿Sigues ahí? Aún no hemos procesado tu comprobante. Si necesitas ayuda escribe 'Soporte'.")
            logger.info("Recordatorio enviado a %s", user_chat_id)
        except Exception as e:
            logger.error("Error enviando recordatorio a %s: %s", user_chat_id, e)
    pending_checks.pop(user_chat_id, None)

# ---------------- CALLBACK de admins (aprobar/rechazar) ----------------
async def validar_transaccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if "|" not in data:
        await query.edit_message_caption(caption="Comando inválido")
        return
    accion, user_chat_id = data.split("|")
    user_chat_id = int(user_chat_id)
    admin_id = query.from_user.id

    df = read_user_df()

    if accion == "aprobar":
        # buscamos la fila correspondiente (ultima con ChatID y Estado comprobante)
        mask = (df["ChatID"] == user_chat_id) & (df["Estado"].str.contains("Comprobante|Esperando", na=False))
        if not mask.any():
            # fallback: encontrar ultima fila por chat
            mask = (df["ChatID"] == user_chat_id)
        if mask.any():
            idx = df[mask].index[-1]
            # Si no tiene CodigoUsuario, generar uno (solo la primera vez)
            if not str(df.at[idx, "CodigoUsuario"]).strip():
                codigo = generate_unique_code(df)
                df.at[idx, "CodigoUsuario"] = codigo
            else:
                codigo = str(df.at[idx, "CodigoUsuario"])
            df.at[idx, "Estado"] = "Aprobado"
            df.at[idx, "AdminComentario"] = f"Aprobado por admin {admin_id} el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            # calcular fecha de pago (10 dias desde FechaRegistro si existe, else ahora+10)
            try:
                fecha_reg = pd.to_datetime(df.at[idx, "FechaRegistro"])
                fecha_pago = (fecha_reg + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
            except Exception:
                fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
            df.at[idx, "FechaPago"] = fecha_pago
            save_user_df(df)
        else:
            logger.warning("No se encontró fila para aprobar %s", user_chat_id)
            codigo = generate_unique_code(df)
            # append row minimal
            nuevo = {
                "RecordID": int(datetime.now().timestamp()),
                "ChatID": user_chat_id, "Nombre": "", "Cédula": "", "Monto": 0,
                "Referido": "", "CodigoUsuario": codigo,
                "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "FechaPago": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
                "ComprobanteFileID": "", "Estado": "Aprobado",
                "AdminComentario": f"Aprobado por admin {admin_id}"
            }
            df = pd.concat([df, pd.DataFrame([nuevo])], ignore_index=True)
            save_user_df(df)

        # Enviar mensaje al usuario con codigo y detalles
        try:
            monto = df.at[idx, "Monto"] if mask.any() else 0
            monto = int(monto) if str(monto).isdigit() else monto
            ganancia = int(monto * 1.9) if isinstance(monto, (int, float)) else ""
            text = (
                f"✅ Transacción aprobada!\n\n"
                f"🔑 Tu código de usuario es: *INV-{codigo}*\n\n"
                f"🏦 Valor a recibir: *{ganancia:,}* COP\n"
                f"📅 Fecha estimada de pago: *{fecha_pago}*\n\n"
                "Nota: los referidos se consignan el mismo día a partir de las 7:00 PM (excepto domingos)."
            )
            await context.bot.send_message(chat_id=user_chat_id, text=text, parse_mode="Markdown")
            await context.bot.send_message(chat_id=user_chat_id, text="¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
        except Exception as e:
            logger.error("Error notificando aprobado a %s: %s", user_chat_id, e)

        # Notificar admin
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"Has aprobado la transacción de {user_chat_id}. Código: INV-{codigo}")
        except Exception:
            pass

        # Limpiar pending
        pending_checks.pop(user_chat_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif accion == "rechazar":
        # pedimos motivo al admin
        pending_rejects[admin_id] = user_chat_id
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"Escribe el *motivo* del rechazo para el usuario `{user_chat_id}`. Envia el texto ahora:", parse_mode="Markdown")
        except Exception as e:
            logger.error("Error pidiendo motivo al admin %s: %s", admin_id, e)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

# ---------------- Admin envía motivo del rechazo ----------------
async def admin_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in pending_rejects:
        return  # no estamos esperando motivo
    user_chat_id = pending_rejects.pop(admin_id)
    motivo = update.message.text.strip()
    df = read_user_df()
    mask = (df["ChatID"] == user_chat_id) & (df["Estado"].str.contains("Comprobante|Enviado|Esperando", na=False))
    if mask.any():
        idx = df[mask].index[-1]
        df.at[idx, "Estado"] = "Rechazado"
        df.at[idx, "AdminComentario"] = f"Rechazado por admin {admin_id}: {motivo}"
        save_user_df(df)
    else:
        logger.warning("No se encontró fila para marcar rechazo de %s", user_chat_id)

    try:
        await context.bot.send_message(chat_id=user_chat_id, text=f"❌ Tu comprobante fue rechazado.\nMotivo: {motivo}\nPor favor revisa y vuelve a enviarlo.")
    except Exception as e:
        logger.error("Error notificando rechazo a %s: %s", user_chat_id, e)
    try:
        await context.bot.send_message(chat_id=admin_id, text=f"Motivo enviado y usuario notificado: {user_chat_id}")
    except Exception:
        pass

# ---------------- Menu opciones (fijas) ----------------
async def menu_opciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    opcion = update.message.text.strip().lower()

    if opcion == "mis referidos":
        df = read_user_df()
        # Encontrar el codigo del usuario por chat
        chat_id = update.effective_chat.id
        mask = (df["ChatID"] == chat_id) & (df["CodigoUsuario"].astype(str) != "")
        if mask.any():
            codigo = df[mask]["CodigoUsuario"].values[-1]
            referidos = df[df["Referido"] == codigo]
            if referidos.empty:
                await update.message.reply_text("📋 No tienes referidos registrados.", reply_markup=main_menu_keyboard())
            else:
                lista = "\n".join([f"{row['Nombre']} - {row['Cédula']} (Código: {row.get('CodigoUsuario','')})" for _, row in referidos.iterrows()])
                await update.message.reply_text(f"📋 Tus referidos:\n\n{lista}", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("📋 No encontrado tu código. Asegúrate de estar registrado.", reply_markup=main_menu_keyboard())

    elif opcion == "soporte":
        await update.message.reply_text(
            "📞 *Soporte*\n\n"
            "✉️ Correo: vortex440@gmail.com\n"
            "📱 WhatsApp 1: https://wa.link/oceivm\n"
            "📱 WhatsApp 2: https://wa.link/istt7e",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    elif opcion in ("horarios de atención", "horarios de atencion", "horarios"):
        await update.message.reply_text(
            "🕑 *Horarios de Atención*\n\n"
            "📅 Lunes a Sábado: 8:00 AM - 7:00 PM\n"
            "📅 Domingo: 8:00 AM - 12:00 PM",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    elif opcion in ("sí", "si"):
        await update.message.reply_text("Perfecto, ¿qué deseas hacer?", reply_markup=main_menu_keyboard())
    elif opcion == "no" or opcion == "salir":
        await update.message.reply_text("🙏 Gracias por confiar en nosotros. Nos vemos en 10 días con tu pago (o antes si tienes referidos).", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    elif opcion == "volver al menú":
        await start(update, context)
    elif opcion == "nueva inversión" or opcion == "nueva inversion":
        return await nueva_inversion(update, context)
    else:
        # Mostrar teclado principal
        await update.message.reply_text("👉 Elige una opción:", reply_markup=main_menu_keyboard())

    return MENU_OPCIONES

# ---------------- ADMIN: Broadcast ----------------
async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("No autorizado.")
        return ConversationHandler.END
    await update.message.reply_text("Envía la imagen (o escribe 'NO' si solo texto). Luego enviarás el texto del mensaje.")
    return ADMIN_BROADCAST_GET_MEDIA

async def admin_broadcast_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = ("photo", update.message.photo[-1].file_id)
    elif update.message.document:
        file_id = ("document", update.message.document.file_id)
    elif update.message.text and update.message.text.strip().upper() == "NO":
        file_id = None
    context.user_data["admin_broadcast_file"] = file_id
    await update.message.reply_text("Ahora escribe el texto que deseas enviar a todos los usuarios:")
    return ADMIN_BROADCAST_CONFIRM

async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END
    texto = update.message.text or ""
    file_info = context.user_data.get("admin_broadcast_file", None)
    df = read_user_df()
    chat_ids = df["ChatID"].dropna().unique().tolist()
    sent = 0
    failed = 0
    for cid in chat_ids:
        try:
            if file_info:
                ftype, fid = file_info
                if ftype == "photo":
                    await context.bot.send_photo(chat_id=int(cid), photo=fid, caption=texto)
                else:
                    await context.bot.send_document(chat_id=int(cid), document=fid, caption=texto)
            else:
                await context.bot.send_message(chat_id=int(cid), text=texto)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error("Error broadcast a %s: %s", cid, e)
            failed += 1
    await update.message.reply_text(f"Broadcast enviado. Exitosos: {sent}, fallidos: {failed}")
    return ConversationHandler.END

# ---------------- NUEVA INVERSIÓN ----------------
async def nueva_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    amounts = [["200.000", "250.000"], ["300.000", "350.000"], ["400.000", "450.000"], ["500.000"]]
    keyboard = ReplyKeyboardMarkup(amounts, one_time_keyboard=True, resize_keyboard=True)
    try:
        if FILE_ID_MONTOS:
            await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_MONTOS)
    except Exception:
        pass
    await update.message.reply_text("💰 Selecciona el nuevo monto:", reply_markup=keyboard)
    return NUEVA_INVERSION_MONTO

async def recibir_nueva_inversion_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        monto = int(text.replace(".", "").strip())
    except Exception:
        await update.message.reply_text("❌ Ingresa un monto válido.")
        return NUEVA_INVERSION_MONTO
    if not (200000 <= monto <= 500000):
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000.")
        return NUEVA_INVERSION_MONTO

    context.user_data["nueva_monto"] = monto
    fecha_pago = datetime.now() + timedelta(days=10)
    context.user_data["nueva_fecha_pago"] = fecha_pago

    await update.message.reply_text(
        f"✅ Envía el comprobante de {monto:,} COP.\nFecha de pago estimada: {fecha_pago.strftime('%Y-%m-%d')}",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Volver al menú")]], one_time_keyboard=True, resize_keyboard=True)
    )
    try:
        if FILE_ID_NEQUI:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=FILE_ID_NEQUI)
    except Exception:
        pass
    return NUEVA_INVERSION_COMPROBANTE

async def recibir_nueva_inversion_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Envía una foto o documento como comprobante.")
        return NUEVA_INVERSION_COMPROBANTE

    monto = context.user_data.get("nueva_monto", 0)
    fecha_pago = context.user_data.get("nueva_fecha_pago", datetime.now() + timedelta(days=10))

    df = read_user_df()
    # buscar fila del usuario para anexar nueva inversion
    mask = (df["ChatID"] == chat_id)
    if mask.any():
        idx = df[mask].index[-1]
        # buscar un número i para columnas "NuevaInversion i" libres
        i = 1
        while f"NuevaInversion {i}" in df.columns:
            i += 1
        df[f"NuevaInversion {i}"] = ""
        df[f"FechaPagoNueva {i}"] = ""
        df.at[idx, f"NuevaInversion {i}"] = monto
        df.at[idx, f"FechaPagoNueva {i}"] = fecha_pago.strftime("%Y-%m-%d")
        # marcar estado y guardar comprobante
        df.at[idx, "ComprobanteFileID"] = file_id
        df.at[idx, "Estado"] = "Comprobante enviado (Nueva Inversión)"
        save_user_df(df)
    else:
        # si no existe usuario, crear fila mínima con referido vacio
        nuevo = {
            "RecordID": int(datetime.now().timestamp()),
            "ChatID": chat_id, "Nombre": context.user_data.get("nombre", ""),
            "Cédula": context.user_data.get("cedula", ""), "Monto": monto,
            "Referido": context.user_data.get("referido", "Ninguno"), "CodigoUsuario": "",
            "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago.strftime("%Y-%m-%d"), "ComprobanteFileID": file_id,
            "Estado": "Comprobante enviado (Nueva Inversión)", "AdminComentario": ""
        }
        df = pd.concat([df, pd.DataFrame([nuevo])], ignore_index=True)
        save_user_df(df)

    # Notificar admins
    caption = f"📩 Nueva inversión {monto:,} COP\nChatID: {chat_id}"
    keyboard = [
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{chat_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{chat_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=markup)
        except Exception:
            try:
                await context.bot.send_document(chat_id=admin_id, document=file_id, caption=caption, reply_markup=markup)
            except Exception as e:
                logger.error("No pude enviar comprobante nueva inversion al admin %s: %s", admin_id, e)

    await update.message.reply_text("⏳ Tu nueva inversión será validada en 5-10 minutos.", reply_markup=main_menu_keyboard())
    return MENU_OPCIONES

# ---------------- MAIN ----------------
def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN no encontrado en variables de entorno.")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # Conversation principal
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto)],
            CONFIRMAR_INVERSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_inversion)],
            CODIGO_REFERIDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_referido)],
            CONFIRMAR_REGISTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_registro)],
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)],
            CEDULA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cedula)],
            CONFIRMAR_DATOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_datos)],
            ESPERAR_COMPROBANTE: [MessageHandler(filters.ALL & ~filters.COMMAND, recibir_comprobante)],
            MENU_OPCIONES: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_opciones)],
            ADMIN_BROADCAST_GET_MEDIA: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_broadcast_receive_media)],
            ADMIN_BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_confirm)],
            # nueva inversion
            NUEVA_INVERSION_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nueva_inversion_monto)],
            NUEVA_INVERSION_COMPROBANTE: [MessageHandler(filters.ALL & ~filters.COMMAND, recibir_nueva_inversion_comprobante)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(validar_transaccion))
    # handler global para motivos de admin (fuera del conversation principal)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reason_handler))
    # admin broadcast
    app.add_handler(CommandHandler("broadcast", admin_broadcast_command))
    # log start
    logger.info("✅ Bot iniciado. Run polling...")

    app.run_polling()

if __name__ == "__main__":
    main()


