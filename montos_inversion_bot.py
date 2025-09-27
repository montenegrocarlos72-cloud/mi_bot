#!/usr/bin/env python3
# montos_inversion_bot.py
"""
Bot Telegram - flujo completo de inversiones con Google Sheets (gspread).
No usa pandas para evitar problemas de compatibilidad en entornos como Railway.
"""

import os
import json
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("montos_bot")

# ---------------- Config desde ENV ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
FILE_ID_MONTOS = os.getenv("FILE_ID_MONTOS", "")  # file id de la imagen de montos
FILE_ID_NEQUI = os.getenv("FILE_ID_NEQUI", "")    # file id de la cuenta nequi
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")     # ID de Google Sheet
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # JSON string

if not TOKEN:
    logger.error("Falta TELEGRAM_TOKEN en variables de entorno. Abortando.")
    raise SystemExit(1)

# ---------------- Conversation states ----------------
(
    MONTO, CONFIRMAR_INVERSION, CODIGO_REFERIDO, CONFIRMAR_REGISTRO,
    NOMBRE, CEDULA, CONFIRMAR_DATOS, ESPERAR_COMPROBANTE, MENU_OPCIONES,
    ADMIN_BROADCAST_GET_MEDIA, ADMIN_BROADCAST_CONFIRM, ADMIN_REJECTION_REASON,
    NUEVA_INVERSION_MONTO, NUEVA_INVERSION_COMPROBANTE
) = range(14)

# ---------------- Sheet columns (orden y nombres) ----------------
HEADER = [
    "Nombre", "Cédula", "Monto", "Referido", "CodigoUsuario",
    "ChatID", "FechaRegistro", "FechaPago", "ComprobanteFileID",
    "Estado", "AdminComentario", "InversionID"  # InversionID para identificar inversiones si hace falta
]

# ---------------- Runtime memory ----------------
pending_rejects: Dict[int, int] = {}   # admin_id -> user_chat_id (esperando motivo)
pending_checks: Dict[int, float] = {}  # user_chat_id -> timestamp (cuando envió comprobante)
# optional: you could persist pending checks in sheet, but runtime is ok for reminders

# ---------------- Google Sheets helper ----------------
gc = None
sheet = None

def init_gsheets_client():
    global gc, sheet
    if not GOOGLE_SHEETS_CREDENTIALS:
        logger.warning("GOOGLE_SHEETS_CREDENTIALS no está definida. Google Sheets deshabilitado.")
        return None
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        )
        gc = gspread.authorize(creds)
        if SPREADSHEET_ID:
            sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
            # Ensure header exists
            try:
                header_row = sheet.row_values(1)
                if not header_row or header_row[0] == "":
                    sheet.insert_row(HEADER, index=1)
                    logger.info("Se creó la cabecera en la sheet.")
            except APIError as e:
                logger.error("Error accediendo/creando header en Sheet: %s", e)
        else:
            sheet = None
            logger.warning("SPREADSHEET_ID no definido.")
    except Exception as e:
        logger.exception("Error inicializando Google Sheets: %s", e)
        gc = None
        sheet = None

def sheet_get_all_records() -> List[Dict[str, Any]]:
    """Devuelve lista de dicts basados en header. Si sheet no disponible devuelve []."""
    if sheet is None:
        return []
    try:
        return sheet.get_all_records()
    except Exception as e:
        logger.error("Error leyendo registros de sheet: %s", e)
        return []

def ensure_header():
    if sheet is None:
        return
    header_row = sheet.row_values(1)
    if not header_row or header_row[0] == "":
        try:
            sheet.insert_row(HEADER, index=1)
        except Exception as e:
            logger.error("No se pudo insertar header: %s", e)

def append_row_from_dict(d: Dict[str, Any]):
    """Append new row keeping HEADER order. Accepts missing keys."""
    if sheet is None:
        logger.warning("Sheet no inicializada, no se guarda fila.")
        return
    ensure_header()
    row = []
    for h in HEADER:
        val = d.get(h, "")
        row.append(str(val))
    try:
        sheet.append_row(row)
    except Exception as e:
        logger.error("Error appending row to sheet: %s", e)

def update_first_matching_row(match_column: str, match_value: Any, updates: Dict[str, Any]):
    """
    Busca la primera fila donde columna match_column == match_value y actualiza las columnas dadas.
    match_column and headers are case-sensitive to HEADER entries.
    """
    if sheet is None:
        logger.warning("Sheet no inicializada, no se puede actualizar.")
        return False
    try:
        records = sheet_get_all_records()
        if not records:
            return False
        # find header indexes
        header = sheet.row_values(1)
        for idx, rec in enumerate(records, start=2):  # sheet rows start at 1 and header at row 1
            # safe: rec has keys from header
            if str(rec.get(match_column, "")) == str(match_value):
                # update columns
                for k, v in updates.items():
                    if k in header:
                        col = header.index(k) + 1
                        sheet.update_cell(idx, col, str(v))
                return True
        return False
    except Exception as e:
        logger.error("Error updating sheet row: %s", e)
        return False

def find_latest_row_index_by_chatid(chat_id: int) -> Optional[int]:
    """Retorna index de la última fila (num de fila en sheet) para el chat_id, o None."""
    if sheet is None:
        return None
    try:
        records = sheet_get_all_records()
        header = sheet.row_values(1)
        for i in range(len(records)-1, -1, -1):
            rec = records[i]
            if str(rec.get("ChatID", "")) == str(chat_id):
                return i + 2  # +2 porque records starts at row 2
        return None
    except Exception as e:
        logger.error("Error finding latest row by chatid: %s", e)
        return None

# Init client at import/run
init_gsheets_client()

# ---------------- Helpers de texto/teclados ----------------
def format_money(n: int) -> str:
    return f"{n:,}".replace(",", ".")

def amounts_keyboard_reply():
    keyboard = [
        ["200.000", "250.000"],
        ["300.000", "350.000"],
        ["400.000", "450.000"],
        ["500.000"]
    ]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["Mis referidos", "Soporte"], ["Horarios de atención", "Nueva inversión"], ["Salir"]],
        one_time_keyboard=False, resize_keyboard=True
    )

def small_yesno_keyboard():
    return ReplyKeyboardMarkup([["Sí"], ["No"]], one_time_keyboard=True, resize_keyboard=True)

# ---------------- Flow handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Send montos image (file_id) then keyboard
    try:
        if FILE_ID_MONTOS:
            await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_MONTOS)
    except Exception as e:
        logger.warning("No se pudo enviar imagen montos: %s", e)

    await update.message.reply_text(
        "💰 *Montos de inversión disponibles*\n\n"
        "Elige uno de los montos o escribe otro (usa puntos):",
        parse_mode="Markdown",
        reply_markup=amounts_keyboard_reply()
    )
    return MONTO

async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        monto = int(text.replace(".", "").strip())
    except Exception:
        await update.message.reply_text("❌ Ingresa un monto válido con puntos (ej: 200.000).")
        return MONTO

    if not (200000 <= monto <= 500000):
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.")
        return MONTO

    context.user_data["monto"] = monto
    pago = int(monto * 1.9)
    fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"✅ Deseas invertir *{format_money(monto)}* COP?\n"
        f"En 10 días recibirás *{format_money(pago)}* COP (estimado: {fecha_pago}).",
        parse_mode="Markdown",
        reply_markup=small_yesno_keyboard()
    )
    return CONFIRMAR_INVERSION

async def confirmar_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text in ("sí", "si"):
        await update.message.reply_text("🔑 ¿Vienes referido por alguien? (Sí / No)", reply_markup=small_yesno_keyboard())
        return CODIGO_REFERIDO
    else:
        await update.message.reply_text("❌ Gracias por ingresar, vuelve cuando estés seguro.")
        return ConversationHandler.END

async def recibir_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() in ("no", "n"):
        context.user_data["referido"] = "Ninguno"
        await update.message.reply_text("📝 ¿Deseas continuar con el registro? (Sí / No)", reply_markup=small_yesno_keyboard())
        return CONFIRMAR_REGISTRO

    codigo = text.strip()
    # buscar codigo en sheet
    records = sheet_get_all_records()
    if not records:
        await update.message.reply_text("⚠️ Aún no hay registros. Si tienes código intenta más tarde o escribe 'No'.")
        return CODIGO_REFERIDO

    found = False
    for rec in records:
        if str(rec.get("CodigoUsuario", "")) == str(codigo):
            nombre_ref = rec.get("Nombre", "(referente)")
            found = True
            break

    if found:
        context.user_data["referido"] = codigo
        await update.message.reply_text(f"✅ Vienes referido por *{nombre_ref}* (código {codigo}).", parse_mode="Markdown")
        await update.message.reply_text("📝 ¿Deseas continuar con el registro? (Sí / No)", reply_markup=small_yesno_keyboard())
        return CONFIRMAR_REGISTRO
    else:
        await update.message.reply_text("⚠️ Código no válido. Ingresa otro código o escribe 'No'.")
        return CODIGO_REFERIDO

async def confirmar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text in ("sí", "si"):
        await update.message.reply_text("✍️ Escribe tu *nombre completo*:", parse_mode="Markdown")
        return NOMBRE
    else:
        await update.message.reply_text("❌ Gracias, vuelve cuando estés seguro.")
        return ConversationHandler.END

async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = (update.message.text or "").strip()
    await update.message.reply_text("🆔 Ahora escribe tu *número de cédula*:", parse_mode="Markdown")
    return CEDULA

async def recibir_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cedula"] = (update.message.text or "").strip()
    nombre = context.user_data.get("nombre", "")
    cedula = context.user_data.get("cedula", "")
    await update.message.reply_text(
        f"✅ Confirma tus datos:\n\n👤 {nombre}\n🆔 {cedula}\n\n¿Son correctos?",
        reply_markup=small_yesno_keyboard()
    )
    return CONFIRMAR_DATOS

async def confirmar_datos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text not in ("sí", "si"):
        await update.message.reply_text("❌ Corrige tus datos. Escribe tu nombre completo:")
        return NOMBRE

    nombre = context.user_data.get("nombre", "")
    cedula = context.user_data.get("cedula", "")
    referido = context.user_data.get("referido", "Ninguno")
    monto = context.user_data.get("monto", 0)
    chat_id = update.effective_chat.id

    fecha_registro = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")

    # preparar fila
    nuevo = {
        "Nombre": nombre,
        "Cédula": cedula,
        "Monto": monto,
        "Referido": referido,
        "CodigoUsuario": "",  # se generará al aprobar por admin
        "ChatID": chat_id,
        "FechaRegistro": fecha_registro,
        "FechaPago": fecha_pago,
        "ComprobanteFileID": "",
        "Estado": "Esperando comprobante",
        "AdminComentario": "",
        "InversionID": f"INV-{int(datetime.now().timestamp())}"
    }
    append_row_from_dict(nuevo)

    await update.message.reply_text(
        f"🎉 Registro exitoso, {nombre}!\n\n"
        f"Envía el comprobante a la cuenta mostrada a continuación. Fecha estimada de pago: *{fecha_pago}*",
        parse_mode="Markdown"
    )
    try:
        if FILE_ID_NEQUI:
            await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_NEQUI)
    except Exception as e:
        logger.warning("Error enviando imagen NEQUI: %s", e)

    # guardar pending para recordatorio
    pending_checks[chat_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))

    await update.message.reply_text("¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
    return MENU_OPCIONES

# ---------------- Recibir comprobante (registro o inversión) ----------------
async def recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # aceptar foto o documento
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
    # actualizar última fila de este chatId con comprobante
    idx = find_latest_row_index_by_chatid(chat_id)
    if idx:
        # actualizar ComprobanteFileID y Estado
        header = sheet.row_values(1)
        try:
            col_file = header.index("ComprobanteFileID") + 1
            col_estado = header.index("Estado") + 1
            sheet.update_cell(idx, col_file, file_id)
            sheet.update_cell(idx, col_estado, "Comprobante enviado")
        except Exception as e:
            logger.error("Error actualizando comprobante en sheet: %s", e)
    else:
        # no hay fila: crear una fila mínima
        nuevo = {
            "Nombre": context.user_data.get("nombre", ""),
            "Cédula": context.user_data.get("cedula", ""),
            "Monto": context.user_data.get("monto", 0),
            "Referido": context.user_data.get("referido", "Ninguno"),
            "CodigoUsuario": context.user_data.get("codigo_usuario", ""),
            "ChatID": chat_id,
            "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "ComprobanteFileID": file_id,
            "Estado": "Comprobante enviado",
            "AdminComentario": "",
            "InversionID": f"INV-{int(datetime.now().timestamp())}"
        }
        append_row_from_dict(nuevo)

    # armar caption para admins
    nombre = context.user_data.get("nombre", "(no registrado)")
    ced = context.user_data.get("cedula", "(no registrado)")
    monto = context.user_data.get("monto", 0)
    caption = (
        f"📩 *Nuevo comprobante recibido*\n\n"
        f"👤 {nombre}\n"
        f"🆔 {ced}\n"
        f"💰 {format_money(monto)} COP\n\n"
        f"ChatID: `{chat_id}`"
    )

    # inline keyboard para admins: aprobar/rechazar
    keyboard = [
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{chat_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{chat_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    # enviar a cada admin
    for admin_id in ADMIN_IDS:
        try:
            if file_type == "photo":
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
            else:
                await context.bot.send_document(chat_id=admin_id, document=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.error("Error enviando comprobante a admin %s: %s", admin_id, e)

    await update.message.reply_text("⏳ Tu comprobante fue enviado. Por favor espera 5-10 minutos mientras lo validamos.")
    pending_checks[chat_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Recordatorio si está pendiente ----------------
async def check_pending_after_delay(context: ContextTypes.DEFAULT_TYPE, user_chat_id: int, delay_seconds: int = 600):
    await asyncio.sleep(delay_seconds)
    # comprobar si aún no tiene CodigoUsuario
    recs = sheet_get_all_records()
    pending = False
    for r in recs:
        if str(r.get("ChatID", "")) == str(user_chat_id) and not r.get("CodigoUsuario"):
            pending = True
            break
    if pending:
        try:
            await context.bot.send_message(chat_id=user_chat_id, text="¿Sigues ahí? Aún no hemos procesado tu comprobante. Si necesitas ayuda escribe 'Soporte'.")
            logger.info("Recordatorio enviado a %s", user_chat_id)
        except Exception as e:
            logger.error("Error enviando recordatorio a %s: %s", user_chat_id, e)
    pending_checks.pop(user_chat_id, None)

# ---------------- CallbackQueries de admins (aprobar / rechazar) ----------------
async def manejar_callback_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if "|" not in data:
        await query.edit_message_caption(caption="Comando inválido")
        return

    accion, chat_id_str = data.split("|", 1)
    try:
        user_chat_id = int(chat_id_str)
    except:
        await query.edit_message_caption(caption="ChatID inválido")
        return

    admin_id = query.from_user.id

    if accion == "aprobar":
        # buscar la última fila del usuario
        recs = sheet_get_all_records()
        found_idx = None
        for i in range(len(recs)-1, -1, -1):
            if str(recs[i].get("ChatID", "")) == str(user_chat_id):
                found_idx = i + 2  # row number
                break

        if found_idx:
            header = sheet.row_values(1)
            # generar codigo si no existe
            codigo_col = header.index("CodigoUsuario") + 1
            monto_col = header.index("Monto") + 1
            estado_col = header.index("Estado") + 1
            adminc_col = header.index("AdminComentario") + 1
            try:
                existing_codigo = sheet.cell(found_idx, codigo_col).value
            except Exception:
                existing_codigo = ""
            if not existing_codigo:
                codigo = str(random.randint(1000, 9999))
                try:
                    sheet.update_cell(found_idx, codigo_col, codigo)
                except Exception as e:
                    logger.error("Error escribiendo codigo en sheet: %s", e)
            else:
                codigo = existing_codigo
            # monto
            try:
                monto_val = sheet.cell(found_idx, monto_col).value
                monto = int(str(monto_val)) if monto_val else 0
            except Exception:
                monto = 0
            # update estado & admin comment
            try:
                sheet.update_cell(found_idx, estado_col, "Aprobado")
                sheet.update_cell(found_idx, adminc_col, f"Aprobado por admin {admin_id} el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                logger.error("Error actualizando estado/admincomment: %s", e)

            fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
            pago = int(monto * 1.9)

            # Notificar usuario
            try:
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        f"✅ *Transacción aprobada*\n\n"
                        f"🔑 Tu código de usuario: *INV-{codigo}*\n"
                        f"Has invertido: *{format_money(monto)}* COP\n"
                        f"Recibirás: *{format_money(pago)}* COP\n"
                        f"Fecha estimada de pago: *{fecha_pago}*\n\n"
                        f"📌 Recuerda: los referidos se consignan el mismo día a partir de las 7:00 PM (excepto domingos)."
                    ),
                    parse_mode="Markdown"
                )
                await context.bot.send_message(chat_id=user_chat_id, text="¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
            except Exception as e:
                logger.error("Error notificando usuario aprobado: %s", e)

            # Notify admin
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"Has aprobado la transacción de {user_chat_id}. Código: INV-{codigo}")
            except Exception:
                pass

            # Remove inline keyboard on admin message
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # Clear pending check
            pending_checks.pop(user_chat_id, None)
        else:
            await query.edit_message_caption(caption="No se encontró registro del usuario para aprobar.")
            return

    elif accion == "rechazar":
        # set pending reason for this admin
        pending_rejects[admin_id] = user_chat_id
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"Envía ahora el *motivo* del rechazo para el usuario `{user_chat_id}`:", parse_mode="Markdown")
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.error("Error pidiendo motivo al admin %s: %s", admin_id, e)

# ---------------- Admin envia motivo de rechazo (texto) ----------------
async def admin_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in pending_rejects:
        return  # not expecting reason
    user_chat_id = pending_rejects.pop(admin_id)
    motivo = (update.message.text or "").strip()

    # update latest user row -> Estado = Rechazado, AdminComentario = motivo
    recs = sheet_get_all_records()
    found_idx = None
    for i in range(len(recs)-1, -1, -1):
        if str(recs[i].get("ChatID", "")) == str(user_chat_id):
            found_idx = i + 2
            break
    if found_idx:
        header = sheet.row_values(1)
        estado_col = header.index("Estado") + 1
        adminc_col = header.index("AdminComentario") + 1
        try:
            sheet.update_cell(found_idx, estado_col, "Rechazado")
            sheet.update_cell(found_idx, adminc_col, f"Rechazado por admin {admin_id}: {motivo}")
        except Exception as e:
            logger.error("Error actualizando rechazo en sheet: %s", e)

    # notify user
    try:
        await context.bot.send_message(chat_id=user_chat_id, text=f"❌ Tu comprobante fue rechazado.\nMotivo: {motivo}\nPor favor revisa y vuelve a enviarlo.")
    except Exception as e:
        logger.error("Error notificando rechazo a %s: %s", user_chat_id, e)

    try:
        await context.bot.send_message(chat_id=admin_id, text=f"Motivo enviado y usuario notificado: {user_chat_id}")
    except Exception:
        pass

# ---------------- Menu opciones fijo ----------------
async def menu_opciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    chat_id = update.effective_chat.id

    if text == "mis referidos":
        # obtener codigo del usuario desde sheet (última fila)
        recs = sheet_get_all_records()
        codigo = ""
        for r in reversed(recs):
            if str(r.get("ChatID", "")) == str(chat_id):
                codigo = r.get("CodigoUsuario", "")
                break
        if not codigo:
            await update.message.reply_text("No tienes código asignado aún. Regístrate y espera aprobación.")
            return MENU_OPCIONES
        # buscar referidos
        referidos = [r for r in recs if str(r.get("Referido", "")) == str(codigo)]
        if not referidos:
            await update.message.reply_text("📋 No tienes referidos registrados.")
        else:
            lista = []
            for r in referidos:
                monto = r.get("Monto", "")
                try:
                    monto_txt = format_money(int(monto)) if monto else "0"
                except:
                    monto_txt = str(monto)
                lista.append(f"{r.get('Nombre','(sin nombre)')} - {r.get('Cédula','')} (Monto: {monto_txt})")
            await update.message.reply_text("📋 Tus referidos:\n\n" + "\n".join(lista))
        return MENU_OPCIONES

    if text == "soporte":
        await update.message.reply_text(
            "📞 *Soporte*\n\n✉️ Correo: vortex440@gmail.com\n📱 WhatsApp 1: https://wa.link/oceivm\n📱 WhatsApp 2: https://wa.link/istt7e",
            parse_mode="Markdown"
        )
        return MENU_OPCIONES

    if text in ("horarios de atención", "horarios de atencion", "horarios"):
        await update.message.reply_text(
            "🕑 *Horarios de Atención*\n\n📅 Lunes a Sábado: 8:00 AM - 7:00 PM\n📅 Domingo: 8:00 AM - 12:00 PM",
            parse_mode="Markdown"
        )
        return MENU_OPCIONES

    if text in ("sí", "si"):
        await update.message.reply_text("Perfecto, ¿qué deseas hacer?\nOpciones: Mis referidos / Soporte / Horarios de atención / Nueva inversión / Salir")
        return MENU_OPCIONES

    if text == "salir" or text == "no":
        await update.message.reply_text("🙏 Gracias por confiar en nosotros. Nos vemos en 10 días con tu pago (o antes si tienes referidos).")
        return ConversationHandler.END

    if text in ("volver al menú",):
        await start(update, context)
        return MONTO

    if text in ("nueva inversión", "nueva inversion"):
        # reenviar montos
        try:
            if FILE_ID_MONTOS:
                await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_MONTOS)
        except Exception:
            pass
        await update.message.reply_text("💰 Selecciona el nuevo monto:", reply_markup=amounts_keyboard_reply())
        return NUEVA_INVERSION_MONTO

    await update.message.reply_text("👉 Elige una opción:", reply_markup=main_menu_keyboard())
    return MENU_OPCIONES

# ---------------- Admin broadcast ----------------
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

    file_info = None
    if update.message.photo:
        file_info = ("photo", update.message.photo[-1].file_id)
    elif update.message.document:
        file_info = ("document", update.message.document.file_id)
    elif update.message.text and update.message.text.strip().upper() == "NO":
        file_info = None

    context.user_data["admin_broadcast_file"] = file_info
    await update.message.reply_text("Ahora escribe el texto que deseas enviar a todos los usuarios:")
    return ADMIN_BROADCAST_CONFIRM

async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END
    texto = update.message.text or ""
    file_info = context.user_data.get("admin_broadcast_file")
    recs = sheet_get_all_records()
    chat_ids = list({r.get("ChatID") for r in recs if r.get("ChatID")})

    sent = 0
    failed = 0
    for cid in chat_ids:
        try:
            if not cid:
                continue
            if file_info:
                ftype, fid = file_info
                if ftype == "photo":
                    await context.bot.send_photo(chat_id=int(cid), photo=fid, caption=texto)
                else:
                    await context.bot.send_document(chat_id=int(cid), document=fid, caption=texto)
            else:
                await context.bot.send_message(chat_id=int(cid), text=texto)
            sent += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.error("Error broadcast a %s: %s", cid, e)
            failed += 1

    await update.message.reply_text(f"Broadcast enviado. Exitosos: {sent}, fallidos: {failed}")
    return ConversationHandler.END

# ---------------- Nueva inversion flow ----------------
async def recibir_nueva_inversion_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        monto = int(text.replace(".", "").strip())
    except Exception:
        await update.message.reply_text("❌ Ingresa un monto válido.")
        return NUEVA_INVERSION_MONTO
    if not (200000 <= monto <= 500000):
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.")
        return NUEVA_INVERSION_MONTO

    context.user_data["nueva_monto"] = monto
    fecha_pago = datetime.now() + timedelta(days=10)
    context.user_data["nueva_fecha_pago"] = fecha_pago

    await update.message.reply_text(
        f"✅ Envía el comprobante de {format_money(monto)} COP.\n"
        f"Fecha de pago estimada: {fecha_pago.strftime('%Y-%m-%d')}"
    )
    try:
        if FILE_ID_NEQUI:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=FILE_ID_NEQUI)
    except Exception:
        pass
    return NUEVA_INVERSION_COMPROBANTE

async def recibir_nueva_inversion_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # prácticamente igual que recibir_comprobante pero agrega una nueva fila de inversión
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
        return NUEVA_INVERSION_COMPROBANTE

    chat_id = update.effective_chat.id
    monto = context.user_data.get("nueva_monto", 0)
    fecha_pago = context.user_data.get("nueva_fecha_pago", datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")

    nuevo = {
        "Nombre": context.user_data.get("nombre", ""),
        "Cédula": context.user_data.get("cedula", ""),
        "Monto": monto,
        "Referido": context.user_data.get("referido", "Ninguno"),
        "CodigoUsuario": context.user_data.get("codigo_usuario", ""),
        "ChatID": chat_id,
        "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "FechaPago": fecha_pago,
        "ComprobanteFileID": file_id,
        "Estado": "Comprobante enviado",
        "AdminComentario": "",
        "InversionID": f"INV-{int(datetime.now().timestamp())}"
    }
    append_row_from_dict(nuevo)

    # enviar a admins con botones
    caption = f"📩 *Nueva inversión recibida*\n\nMonto: {format_money(monto)} COP\nChatID: `{chat_id}`"
    keyboard = [
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar|{chat_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar|{chat_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    for admin_id in ADMIN_IDS:
        try:
            if file_type == "photo":
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
            else:
                await context.bot.send_document(chat_id=admin_id, document=file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.error("Error enviando nueva inversion a admin %s: %s", admin_id, e)

    await update.message.reply_text("⏳ Tu nueva inversión será validada en 5-10 minutos.")
    pending_checks[chat_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Fallback / error handler simple ----------------
async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("No entendí eso. Usa /start para comenzar o el menú.")

# ---------------- MAIN ----------------
def main():
    init_gsheets_client()  # intentar inicializar (de nuevo) en el inicio
    app = ApplicationBuilder().token(TOKEN).build()

    # Conversation / reglas
    conv = ConversationHandler(
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

            # Admin broadcast
            ADMIN_BROADCAST_GET_MEDIA: [MessageHandler((filters.PHOTO | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND, admin_broadcast_receive_media)],
            ADMIN_BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_confirm)],

            # Nueva inversión
            NUEVA_INVERSION_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nueva_inversion_monto)],
            NUEVA_INVERSION_COMPROBANTE: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, recibir_nueva_inversion_comprobante)],
        },
        fallbacks=[CommandHandler("start", start), MessageHandler(filters.ALL, fallback_handler)],
        per_message=False
    )

    app.add_handler(conv)

    # callback queries (aprobaciones/rechazos)
    app.add_handler(CallbackQueryHandler(manejar_callback_admin))

    # admin reason messages (must be before general text handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reason_handler), group=1)

    # admin broadcast command
    app.add_handler(CommandHandler("broadcast", admin_broadcast_command))

    logger.info("✅ Bot iniciado. Run polling...")
    app.run_polling()

if __name__ == "__main__":
    main()










