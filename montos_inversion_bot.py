# montos_inversion_bot.py
import os
import logging
import asyncio
import random
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Any, List

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, filters
)

# ---------------- CONFIG / AJUSTA SEGÚN TUS VALORES ----------------
# TOKEN: usa variable de entorno TELEGRAM_TOKEN en Railway o localmente
TOKEN = os.environ.get("TELEGRAM_TOKEN")
# ADMIN_IDS: lista de ids numéricos de admins
ADMIN_IDS = [8214551774, 1592839102]  # <- reemplaza con tus admins

# File IDs: (reemplaza por tus file_ids desde Telegram)
FILE_ID_MONTOS = os.environ.get("FILE_ID_MONTOS", "AgACAgE...MONTS_FILEID...")  # imagen montos inversión
FILE_ID_NEQUI = os.environ.get("FILE_ID_NEQUI", "AgACAgE...NEQUI_FILEID...")   # imagen cuenta nequi

# Google Sheet ID: ponlo en env var SHEET_ID o edítalo aquí
SHEET_ID = os.environ.get("SHEET_ID", None)

# Inactividad (segundos) para enviar "¿sigues ahí?"
INACTIVITY_SECONDS = 300  # 5 minutos

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- GLOBALS ----------------
# Conversational states
(
    MONTO, CONFIRMAR_INVERSION, CODIGO_REFERIDO, CONFIRMAR_REGISTRO,
    NOMBRE, CEDULA, CONFIRMAR_DATOS, ESPERAR_COMPROBANTE, MENU_OPCIONES,
    ADMIN_BROADCAST_GET_MEDIA, ADMIN_BROADCAST_CONFIRM,
    NUEVA_INVERSION_MONTO, NUEVA_INVERSION_COMPROBANTE
) = range(13)

# map admin_id -> record_id (when admin must send reason)
pending_rejects: Dict[int, str] = {}
# map user_id -> timestamp of last activity (for inactivity reminders)
user_last_active: Dict[int, float] = {}
# pending checks map record_id -> timestamp
pending_checks: Dict[str, float] = {}

# Google Sheets client placeholder (se asigna abajo)
GC = None
SHEET = None
WORKSHEET = None

# ---------------- Helper: Google Sheets connection ----------------
def ensure_google_creds_file():
    """
    If env var GOOGLE_CREDS_JSON exists (content of JSON), write to credentials.json.
    Else we expect a credentials.json file is present.
    """
    env_json = os.environ.get("GOOGLE_CREDS_JSON")
    if env_json:
        with open("credentials.json", "w", encoding="utf-8") as f:
            f.write(env_json)
        logger.info("credentials.json written from GOOGLE_CREDS_JSON env var.")
    else:
        if not os.path.exists("credentials.json"):
            logger.error("No credentials.json found and GOOGLE_CREDS_JSON not provided.")
            raise FileNotFoundError("credentials.json not found and GOOGLE_CREDS_JSON not set.")

def connect_sheets():
    global GC, SHEET, WORKSHEET
    ensure_google_creds_file()
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    GC = gspread.authorize(creds)
    if not SHEET_ID:
        raise ValueError("SHEET_ID env var not set.")
    SHEET = GC.open_by_key(SHEET_ID)
    WORKSHEET = SHEET.sheet1
    # Ensure header row exists - if empty create headers
    headers = ["RecordID", "UserID", "Nombre", "Cedula", "CodigoUsuario", "ReferidoPor",
               "Monto", "FechaInversion", "FechaPago", "Estado", "ComprobanteFileID", "AdminComentario"]
    try:
        current = WORKSHEET.row_values(1)
        if not current:
            WORKSHEET.append_row(headers)
        else:
            # ensure all headers present (if first row shorter)
            if len(current) < len(headers):
                WORKSHEET.update("A1:L1", [headers])
    except Exception as e:
        logger.exception("Error asegurando cabeceras en Google Sheets: %s", e)
        raise

# ---------------- Utilities: sheet operations ----------------
def append_record(data: Dict[str, Any]) -> str:
    """
    data: dict with keys matching headers (RecordID optional)
    Returns record_id
    """
    if WORKSHEET is None:
        raise RuntimeError("WORKSHEET not initialized")
    record_id = data.get("RecordID") or uuid.uuid4().hex[:8]
    row = [
        record_id,
        str(data.get("UserID", "")),
        data.get("Nombre", ""),
        data.get("Cedula", ""),
        data.get("CodigoUsuario", ""),
        data.get("ReferidoPor", ""),
        str(data.get("Monto", "")),
        data.get("FechaInversion", ""),
        data.get("FechaPago", ""),
        data.get("Estado", ""),
        data.get("ComprobanteFileID", ""),
        data.get("AdminComentario", "")
    ]
    WORKSHEET.append_row(row)
    return record_id

def find_row_by_record_id(record_id: str) -> Optional[int]:
    """Return row index (1-based) or None"""
    try:
        cell = WORKSHEET.find(record_id)
        if cell:
            return cell.row
    except Exception:
        return None
    return None

def update_row_by_record_id(record_id: str, updates: Dict[str, Any]):
    """
    updates: mapping header -> value
    """
    # get header mapping
    headers = WORKSHEET.row_values(1)
    row_idx = find_row_by_record_id(record_id)
    if row_idx is None:
        logger.warning("No row found for record_id %s", record_id)
        return False
    for key, value in updates.items():
        if key in headers:
            col = headers.index(key) + 1
            WORKSHEET.update_cell(row_idx, col, value)
    return True

def find_user_rows_by_userid(user_id: int) -> List[int]:
    """Return list of row indices for user"""
    rows = []
    try:
        col_vals = WORKSHEET.col_values(2)  # UserID column (col B)
        for idx, val in enumerate(col_vals, start=1):
            if val and str(val) == str(user_id):
                rows.append(idx)
    except Exception as e:
        logger.error("Error buscando filas por user id: %s", e)
    return rows

def find_user_record(user_id: int) -> Optional[Dict[str, Any]]:
    """Return last record dict for user or None"""
    rows = find_user_rows_by_userid(user_id)
    if not rows:
        return None
    last_row_idx = rows[-1]
    values = WORKSHEET.row_values(last_row_idx)
    headers = WORKSHEET.row_values(1)
    record = {}
    for i, h in enumerate(headers):
        record[h] = values[i] if i < len(values) else ""
    record["_row"] = last_row_idx
    return record

def find_user_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Search for a user row where CodigoUsuario == code, return last match"""
    headers = WORKSHEET.row_values(1)
    try:
        col_vals = WORKSHEET.col_values(headers.index("CodigoUsuario")+1)
    except Exception:
        return None
    for idx in range(len(col_vals), 0, -1):
        if col_vals[idx-1] == str(code):
            values = WORKSHEET.row_values(idx)
            record = {}
            for i, h in enumerate(headers):
                record[h] = values[i] if i < len(values) else ""
            record["_row"] = idx
            return record
    return None

# ---------------- UI helpers ----------------
def main_menu_keyboard():
    keyboard = ReplyKeyboardMarkup(
        [["Nueva inversión", "Mis referidos"], ["Soporte", "Horarios de atención"], ["Salir"]],
        one_time_keyboard=False, resize_keyboard=True
    )
    return keyboard

def yes_no_markup():
    return ReplyKeyboardMarkup([["Sí"], ["No"]], one_time_keyboard=True, resize_keyboard=True)

def montos_markup():
    return ReplyKeyboardMarkup([["200.000","250.000","300.000"], ["350.000","400.000","450.000"], ["500.000"]], one_time_keyboard=True, resize_keyboard=True)

# ---------------- COMMAND / FLOW HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    # first ensure sheet connected
    try:
        if WORKSHEET is None:
            connect_sheets()
    except Exception as e:
        logger.exception("Error conectando Google Sheets: %s", e)
        await update.message.reply_text("⚠️ Error interno: no puedo acceder a la base de datos. Contacta al administrador.")
        return ConversationHandler.END

    # send montos image + buttons
    try:
        await context.bot.send_photo(chat_id=user_id, photo=FILE_ID_MONTOS)
    except Exception:
        logger.warning("No pude enviar la imagen de montos (file id inválido?)")
    await update.message.reply_text(
        "💰 *Montos de inversión disponibles*\n\nElige uno de los montos o escribe otro (usa puntos):",
        parse_mode="Markdown",
        reply_markup=montos_markup()
    )
    return MONTO

async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    text = update.message.text.strip()
    # normalize and parse
    try:
        monto = int(text.replace(".", "").strip())
    except Exception:
        await update.message.reply_text("❌ Ingresa un monto válido con puntos (ej: 200.000).", reply_markup=montos_markup())
        return MONTO

    if 200000 <= monto <= 500000:
        context.user_data["monto"] = monto
        ganancia = int(monto * 1.9)  # sumarle 90%
        context.user_data["ganancia"] = ganancia
        await update.message.reply_text(
            f"✅ Deseas invertir *{monto:,}* COP?\nEn 10 días recibirás *{ganancia:,}* COP.",
            parse_mode="Markdown",
            reply_markup=yes_no_markup()
        )
        return CONFIRMAR_INVERSION
    else:
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.", reply_markup=montos_markup())
        return MONTO

async def confirmar_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        # ask if comes referred
        await update.message.reply_text("🔑 ¿Vienes referido por alguien? (Si / No)", reply_markup=yes_no_markup())
        return CODIGO_REFERIDO
    else:
        await update.message.reply_text("❌ Gracias por ingresar, vuelve cuando estés seguro.")
        return ConversationHandler.END

async def recibir_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    codigo = update.message.text.strip()
    if codigo.lower() in ("no", "n"):
        context.user_data["referido"] = ""
        # ask if wants to register
        await update.message.reply_text("📝 ¿Deseas continuar con el registro? (Sí / No)", reply_markup=yes_no_markup())
        return CONFIRMAR_REGISTRO
    else:
        # search in sheet for that code
        record = find_user_by_code = find_user_by_code = find_user_by_code if False else None
        try:
            record = find_user_by_code = None
            # use function
            record = find_user_by_code := (find_user_by_code if False else None)
        except Exception:
            record = None
        # simpler: use find_user_by_code function defined earlier
        record = find_user_by_code(codigo) if 'find_user_by_code' in globals() else None
        if record:
            # referer exists
            await update.message.reply_text(f"✅ Vienes referido por *{record.get('Nombre','(nombre desconocido)')}* (Código: {codigo}).", parse_mode="Markdown")
            context.user_data["referido"] = codigo
            await update.message.reply_text("📝 ¿Deseas continuar con el registro? (Sí / No)", reply_markup=yes_no_markup())
            return CONFIRMAR_REGISTRO
        else:
            await update.message.reply_text("⚠️ Código no válido. Ingresa otro código referidor o escribe NO.", reply_markup=yes_no_markup())
            return CODIGO_REFERIDO

async def confirmar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    if update.message.text.strip().lower() in ("sí", "si"):
        await update.message.reply_text("✍️ Escribe tu *nombre completo*:", parse_mode="Markdown")
        return NOMBRE
    else:
        await update.message.reply_text("❌ Gracias por visitar. Vuelve cuando estés seguro.")
        return ConversationHandler.END

async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    context.user_data["nombre"] = update.message.text.strip()
    await update.message.reply_text("🆔 Ahora escribe tu *número de cédula*:", parse_mode="Markdown")
    return CEDULA

async def recibir_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    context.user_data["cedula"] = update.message.text.strip()
    nombre = context.user_data.get("nombre", "")
    cedula = context.user_data.get("cedula", "")
    await update.message.reply_text(
        f"✅ Confirma tus datos:\n\n👤 Nombre: *{nombre}*\n🆔 Cédula: *{cedula}*\n\n¿Son correctos?",
        parse_mode="Markdown",
        reply_markup=yes_no_markup()
    )
    return CONFIRMAR_DATOS

async def confirmar_datos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    if update.message.text.strip().lower() in ("sí", "si"):
        # Save initial record (Estado: Esperando comprobante)
        nombre = context.user_data.get("nombre", "")
        cedula = context.user_data.get("cedula", "")
        referido = context.user_data.get("referido", "")
        monto = context.user_data.get("monto", 0)
        ahora = datetime.now()
        fecha_pago = (ahora + timedelta(days=10)).strftime("%Y-%m-%d")
        # check if user already has a CodigoUsuario
        existing = find_user_record(user_id= user_id := user_id)
        # existing is function; call appropriately:
        existing = find_user_record(user_id)  # last record for user if any
        codigo_usuario = ""
        if existing and existing.get("CodigoUsuario"):
            codigo_usuario = existing.get("CodigoUsuario")
        else:
            # generate 4-digit unique code (ensure unique scanning sheet)
            while True:
                codigo_usuario = str(random.randint(1000, 9999))
                if not find_user_by_code(codigo_usuario):
                    break
        # append new record
        record = {
            "UserID": user_id,
            "Nombre": nombre,
            "Cedula": cedula,
            "CodigoUsuario": codigo_usuario,
            "ReferidoPor": referido,
            "Monto": monto,
            "FechaInversion": ahora.strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago,
            "Estado": "Esperando comprobante",
            "ComprobanteFileID": "",
            "AdminComentario": ""
        }
        record_id = append_record(record)
        # store record_id in user_data for later reference
        context.user_data["last_record_id"] = record_id
        # send info and nequi image
        await update.message.reply_text(
            f"🎉 Registro inicial exitoso *{nombre}*!\n\nEnvía el comprobante de tu pago a la siguiente cuenta y luego espera la validación.\n\nFecha estimada de pago: *{fecha_pago}* (10 días desde hoy).",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_photo(chat_id=user_id, photo=FILE_ID_NEQUI)
        except Exception:
            logger.warning("No se pudo enviar imagen NEQUI.")
        # mark pending check and start reminder task
        pending_checks[record_id] = datetime.now().timestamp()
        asyncio.create_task(check_pending_after_delay(context, record_id, delay_seconds=600))
        # move to menu
        await update.message.reply_text("⏳ Envía el comprobante (foto o documento).", reply_markup=main_menu_keyboard())
        return ESPERAR_COMPROBANTE
    else:
        await update.message.reply_text("Corrige tus datos. Escribe tu nombre completo:")
        return NOMBRE

# ---------------- RECEPCION DE COMPROBANTE (usuario) ----------------
async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    file_id = None
    file_type = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
    else:
        await update.message.reply_text("⚠️ Envía una imagen o documento como comprobante.", reply_markup=main_menu_keyboard())
        return ESPERAR_COMPROBANTE

    # find last record for this user that is expecting comprobante
    record = find_user_record(user_id)
    if not record:
        # if no record found, create a minimal record so admin can process
        now = datetime.now()
        fecha_pago = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        rec = {
            "UserID": user_id,
            "Nombre": context.user_data.get("nombre",""),
            "Cedula": context.user_data.get("cedula",""),
            "CodigoUsuario": context.user_data.get("codigo_usuario",""),
            "ReferidoPor": context.user_data.get("referido",""),
            "Monto": context.user_data.get("monto",0),
            "FechaInversion": now.strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago,
            "Estado": "Comprobante enviado",
            "ComprobanteFileID": file_id,
            "AdminComentario": ""
        }
        record_id = append_record(rec)
    else:
        record_id = record.get("RecordID") or record.get("RecordId") or WORKSHEET.cell(record["_row"], 1).value
        update_row_by_record_id(record_id, {"ComprobanteFileID": file_id, "Estado": "Comprobante enviado"})

    # notify admins with inline approve/reject buttons (include record_id)
    caption_text = (
        f"📩 *Nuevo comprobante recibido*\n\n"
        f"👤 {context.user_data.get('nombre','(no registrado)')}\n"
        f"🆔 {context.user_data.get('cedula','(no registrado)')}\n"
        f"💰 {context.user_data.get('monto',0):,} COP\n\n"
        f"RecordID: `{record_id}`\n"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{record_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{record_id}")]
    ])
    for admin in ADMIN_IDS:
        try:
            if file_type == "photo":
                await context.bot.send_photo(chat_id=admin, photo=file_id, caption=caption_text, parse_mode="Markdown", reply_markup=markup)
            else:
                await context.bot.send_document(chat_id=admin, document=file_id, caption=caption_text, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            logger.error("Error enviando comprobante a admin %s: %s", admin, e)

    await update.message.reply_text("⏳ Hemos recibido tu comprobante. Se validará en 5 a 10 minutos.", reply_markup=main_menu_keyboard())
    pending_checks[record_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, record_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Verificar pendiente después de delay ----------------
async def check_pending_after_delay(context: ContextTypes.DEFAULT_TYPE, record_id: str, delay_seconds: int = 600):
    await asyncio.sleep(delay_seconds)
    # reload record and check if still without CodigoUsuario or Estado not approved
    try:
        headers = WORKSHEET.row_values(1)
        row_idx = find_row_by_record_id(record_id)
        if not row_idx:
            return
        estado = WORKSHEET.cell(row_idx, headers.index("Estado")+1).value
        if estado and "Aprobado" not in estado:
            # remind user
            user_id = WORKSHEET.cell(row_idx, headers.index("UserID")+1).value
            try:
                await context.bot.send_message(chat_id=int(user_id), text="¿Sigues ahí? Aún no hemos procesado tu comprobante. Si necesitas ayuda escribe 'Soporte'.")
                logger.info("Recordatorio enviado a %s por record %s", user_id, record_id)
            except Exception as e:
                logger.error("Error enviando recordatorio a %s: %s", user_id, e)
    except Exception as e:
        logger.error("Error en check_pending_after_delay: %s", e)
    pending_checks.pop(record_id, None)

# ---------------- CALLBACK DE ADMINS (aprobar/rechazar) ----------------
async def validar_transaccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if "|" not in data:
        await query.edit_message_caption(caption="Comando inválido")
        return
    accion, record_id = data.split("|", 1)
    admin_id = query.from_user.id

    if accion == "aprobar":
        # mark approved and set CodigoUsuario if missing
        row = find_row_by_record_id(record_id)
        if row:
            headers = WORKSHEET.row_values(1)
            codigo_col = headers.index("CodigoUsuario")+1
            estado_col = headers.index("Estado")+1
            monto_col = headers.index("Monto")+1
            fecha_inversion_col = headers.index("FechaInversion")+1
            # get current code
            codigo_actual = WORKSHEET.cell(row, codigo_col).value or ""
            if not codigo_actual:
                # generate unique 4-digit
                while True:
                    newcode = str(random.randint(1000,9999))
                    if not find_user_by_code(newcode):
                        break
                WORKSHEET.update_cell(row, codigo_col, newcode)
                codigo_actual = newcode
            WORKSHEET.update_cell(row, estado_col, "Aprobado")
            # prepare user message
            monto = WORKSHEET.cell(row, monto_col).value or "0"
            fecha_pago = WORKSHEET.cell(row, fecha_inversion_col).value
            try:
                fecha_dt = datetime.strptime(fecha_pago, "%Y-%m-%d %H:%M:%S")
            except Exception:
                fecha_dt = datetime.now()
            fecha_pago_calc = (fecha_dt + timedelta(days=10)).strftime("%Y-%m-%d")
            # notify user
            user_id = int(WORKSHEET.cell(row, headers.index("UserID")+1).value)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ Transacción aprobada!\n\n"
                        f"🔑 Tu código de usuario es: *INV-{codigo_actual}*\n\n"
                        f"📌 Monto: {int(float(monto)):,} COP\n"
                        f"📅 Fecha de pago estimada: *{fecha_pago_calc}*\n\n"
                        f"Nota: los referidos se consignan el mismo día a partir de las 7:00 PM (excepciones domingos)."
                    ),
                    parse_mode="Markdown"
                )
                await context.bot.send_message(chat_id=user_id, text="¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
            except Exception as e:
                logger.error("No se pudo notificar al usuario %s: %s", user_id, e)

            # notify admin
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"Has aprobado la transacción (record {record_id}). Código: INV-{codigo_actual}")
            except Exception:
                pass
            # cleanup pending
            pending_checks.pop(record_id, None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

    elif accion == "rechazar":
        # ask for reason from this admin
        pending_rejects[admin_id] = record_id
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"✍️ Escribe el *motivo* del rechazo para el registro `{record_id}` y lo enviaré al usuario:", parse_mode="Markdown")
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.error("Error pidiendo motivo al admin %s: %s", admin_id, e)

# ---------------- Admin sends reason handler ----------------
async def admin_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in pending_rejects:
        return  # nothing to do
    record_id = pending_rejects.pop(admin_id)
    motivo = update.message.text.strip()
    # update sheet: Estado = Rechazado, AdminComentario = motivo
    update_row_by_record_id(record_id, {"Estado":"Rechazado", "AdminComentario": f"Rechazado por {admin_id}: {motivo}"})
    # notify user
    # find user id
    row_idx = find_row_by_record_id(record_id)
    if row_idx:
        headers = WORKSHEET.row_values(1)
        user_col = headers.index("UserID")+1
        user_id = WORKSHEET.cell(row_idx, user_col).value
        try:
            await context.bot.send_message(chat_id=int(user_id), text=f"❌ Tu comprobante fue rechazado.\nMotivo: {motivo}\nPor favor revisa y vuelve a enviarlo.")
            await context.bot.send_message(chat_id=admin_id, text=f"Motivo enviado y usuario notificado: {user_id}")
        except Exception as e:
            logger.error("Error notificando rechazo: %s", e)
    else:
        await context.bot.send_message(chat_id=admin_id, text=f"No encontré el record {record_id} para notificar al usuario, pero actualicé la hoja.")

# ---------------- MENU OPCIONES (botones fijos) ----------------
async def menu_opciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    opcion = update.message.text.strip().lower()
    if opcion == "mis referidos":
        # get user's own code
        rec = find_user_record(user_id)
        if not rec or not rec.get("CodigoUsuario"):
            await update.message.reply_text("No tienes código asignado aún. Aún no has sido aprobado.")
            return MENU_OPCIONES
        mi_codigo = rec.get("CodigoUsuario")
        # find referidos rows where ReferidoPor == mi_codigo
        headers = WORKSHEET.row_values(1)
        try:
            col_vals = WORKSHEET.col_values(headers.index("ReferidoPor")+1)
        except Exception:
            await update.message.reply_text("Error consultando referidos.")
            return MENU_OPCIONES
        results = []
        for idx, val in enumerate(col_vals, start=1):
            if val == mi_codigo:
                row_vals = WORKSHEET.row_values(idx)
                name = row_vals[2] if len(row_vals) > 2 else ""
                ced = row_vals[3] if len(row_vals) > 3 else ""
                results.append(f"{name} - {ced}")
        if not results:
            await update.message.reply_text("📋 No tienes referidos registrados.")
        else:
            await update.message.reply_text("📋 Tus referidos:\n\n" + "\n".join(results))
    elif opcion == "soporte":
        await update.message.reply_text(
            "📞 *Soporte*\n\n✉️ Correo: vortex440@gmail.com\n📱 WhatsApp 1: https://wa.link/oceivm\n📱 WhatsApp 2: https://wa.link/istt7e",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    elif opcion == "horarios de atención" or opcion == "horarios de atencion":
        await update.message.reply_text(
            "🕑 *Horarios de Atención*\n\n📅 Lunes a Sábado: 8:00 AM - 7:00 PM\n📅 Domingo: 8:00 AM - 12:00 PM",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    elif opcion == "nueva inversión":
        # start nueva inversion flow
        return await nueva_inversion(update, context)
    elif opcion == "sí" or opcion == "si":
        await update.message.reply_text("Perfecto, ¿qué deseas hacer?\n👉 Opciones: Nueva inversión / Mis referidos / Soporte / Horarios de atención / Salir", reply_markup=main_menu_keyboard())
    elif opcion == "no" or opcion == "salir":
        await update.message.reply_text("🙏 Gracias por confiar en nosotros. Nos vemos en 10 días (o antes si tienes referidos).")
        return ConversationHandler.END
    else:
        await update.message.reply_text("👉 Elige una opción:", reply_markup=main_menu_keyboard())
    return MENU_OPCIONES

# ---------------- Nueva inversión (usuario ya registrado) ----------------
async def nueva_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    try:
        await context.bot.send_photo(chat_id=user_id, photo=FILE_ID_MONTOS)
    except Exception:
        pass
    await update.message.reply_text("💰 Selecciona el nuevo monto:", reply_markup=montos_markup())
    return NUEVA_INVERSION_MONTO

async def recibir_nueva_inversion_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    try:
        monto = int(update.message.text.replace(".", "").strip())
    except Exception:
        await update.message.reply_text("❌ Ingresa un monto válido.", reply_markup=montos_markup())
        return NUEVA_INVERSION_MONTO
    if not (200000 <= monto <= 500000):
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.", reply_markup=montos_markup())
        return NUEVA_INVERSION_MONTO
    # save to user_data and ask to send comprobante
    context.user_data["nueva_monto"] = monto
    fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    context.user_data["nueva_fecha_pago"] = fecha_pago
    await update.message.reply_text(f"✅ Envía el comprobante de {monto:,} COP.\nFecha de pago estimada: {fecha_pago}", reply_markup=main_menu_keyboard())
    try:
        await context.bot.send_photo(chat_id=user_id, photo=FILE_ID_NEQUI)
    except Exception:
        pass
    return NUEVA_INVERSION_COMPROBANTE

async def recibir_nueva_inversion_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_last_active[user_id] = datetime.now().timestamp()
    file_id = update.message.photo[-1].file_id if update.message.photo else (update.message.document.file_id if update.message.document else None)
    if not file_id:
        await update.message.reply_text("⚠️ Envía una imagen o documento como comprobante.", reply_markup=main_menu_keyboard())
        return NUEVA_INVERSION_COMPROBANTE
    monto = context.user_data.get("nueva_monto", 0)
    fecha_pago = context.user_data.get("nueva_fecha_pago", (datetime.now()+timedelta(days=10)).strftime("%Y-%m-%d"))
    # append new record that links to existing CodigoUsuario if any
    # get user's CodigoUsuario from latest record
    prev = find_user_record(user_id)
    codigo_usuario = prev.get("CodigoUsuario") if prev else ""
    nombre = prev.get("Nombre") if prev else context.user_data.get("nombre","")
    cedula = prev.get("Cedula") if prev else context.user_data.get("cedula","")
    rec = {
        "UserID": user_id,
        "Nombre": nombre,
        "Cedula": cedula,
        "CodigoUsuario": codigo_usuario,
        "ReferidoPor": prev.get("ReferidoPor","") if prev else "",
        "Monto": monto,
        "FechaInversion": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "FechaPago": fecha_pago,
        "Estado": "Comprobante enviado",
        "ComprobanteFileID": file_id,
        "AdminComentario": ""
    }
    record_id = append_record(rec)
    # notify admins
    caption_text = (
        f"📩 *Nuevo comprobante (Nueva inversión)*\n\n"
        f"👤 {nombre}\n"
        f"🆔 {cedula}\n"
        f"💰 {monto:,} COP\n\n"
        f"RecordID: `{record_id}`\n"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{record_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{record_id}")]
    ])
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin, photo=file_id, caption=caption_text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            try:
                await context.bot.send_document(chat_id=admin, document=file_id, caption=caption_text, parse_mode="Markdown", reply_markup=markup)
            except Exception as e:
                logger.error("Error enviando comprobante nueva inversion a admin %s: %s", admin, e)
    await update.message.reply_text("⏳ Tu nueva inversión será validada en 5-10 minutos.", reply_markup=main_menu_keyboard())
    pending_checks[record_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, record_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Broadcast (admins) ----------------
# Simple conversation for admin broadcast: get media (optional) then message
BCAST_GET_MEDIA, BCAST_GET_TEXT = range(100, 102)
async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("No autorizado.")
        return ConversationHandler.END
    await update.message.reply_text("Envía la imagen (o escribe NO si sólo texto).")
    return BCAST_GET_MEDIA

async def admin_broadcast_get_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text and update.message.text.strip().upper() == "NO":
        context.user_data["bcast_file"] = None
    elif update.message.photo:
        context.user_data["bcast_file"] = ("photo", update.message.photo[-1].file_id)
    elif update.message.document:
        context.user_data["bcast_file"] = ("document", update.message.document.file_id)
    else:
        context.user_data["bcast_file"] = None
    await update.message.reply_text("Ahora escribe el texto que deseas enviar:")
    return BCAST_GET_TEXT

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END
    texto = update.message.text or ""
    file_info = context.user_data.get("bcast_file")
    # get all unique chat ids from sheet
    try:
        col_vals = WORKSHEET.col_values(2)  # UserID column
        unique_ids = list(dict.fromkeys([int(x) for x in col_vals[1:] if x and x.isdigit()]))
    except Exception as e:
        await update.message.reply_text("Error leyendo usuarios desde la hoja.")
        logger.error("Error en broadcast lectura: %s", e)
        return ConversationHandler.END
    sent = 0
    failed = 0
    for cid in unique_ids:
        try:
            if file_info:
                ftype, fid = file_info
                if ftype == "photo":
                    await context.bot.send_photo(chat_id=cid, photo=fid, caption=texto)
                else:
                    await context.bot.send_document(chat_id=cid, document=fid, caption=texto)
            else:
                await context.bot.send_message(chat_id=cid, text=texto)
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error("Error en broadcast a %s: %s", cid, e)
            failed += 1
    await update.message.reply_text(f"Broadcast enviado. Exitosos: {sent}, fallidos: {failed}")
    return ConversationHandler.END

# ---------------- MISC handlers ----------------
async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # keep menu visible
    try:
        await update.message.reply_text("👋 Usa las opciones del menú:", reply_markup=main_menu_keyboard())
    except Exception:
        pass
    return MENU_OPCIONES

# ---------------- START / MAIN ----------------
def build_application():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no establecido en variables de entorno.")
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
            ESPERAR_COMPROBANTE: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, recibir_comprobante)],
            MENU_OPCIONES: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_opciones)],
            NUEVA_INVERSION_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nueva_inversion_monto)],
            NUEVA_INVERSION_COMPROBANTE: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, recibir_nueva_inversion_comprobante)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(validar_transaccion))
    # Admin reason handler (outside conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reason_handler))
    # Broadcast conv
    bcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", admin_broadcast_command)],
        states={
            BCAST_GET_MEDIA: [MessageHandler((filters.PHOTO | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND, admin_broadcast_get_media)],
            BCAST_GET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]
        },
        fallbacks=[],
        per_message=False
    )
    app.add_handler(bcast_conv)

    # small command to force sheet connect
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot funcionando.")
    app.add_handler(CommandHandler("status", cmd_status))

    return app

def main():
    try:
        connect_sheets()
    except Exception as e:
        logger.error("No se pudo conectar a Google Sheets: %s", e)
        # allow app to start but many features won't work

    app = build_application()
    logger.info("✅ Bot iniciado. Run polling...")
    app.run_polling()

if __name__ == "__main__":
    main()








