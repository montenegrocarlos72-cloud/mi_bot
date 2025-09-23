# montos_inversion_bot.py
"""
Bot de inversiones - integración Google Sheets (variable de entorno)
Mantiene todo el flujo: montos, confirmación, referidos, registro,
envío de comprobantes, aprobación/rechazo por admins (con motivo),
nueva inversión, menú fijo, broadcasts de admin, recordatorio por inactividad.
"""

import os
import json
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2 import service_account

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ---------------- CONFIG/ENTORNO ----------------
# Variables de entorno (configurar en Railway)
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "")  # "8214551774,1592839102"
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip().isdigit()]

FILE_ID_MONTOS = os.getenv("FILE_ID_MONTOS", "")  # file_id imagen montos
FILE_ID_NEQUI = os.getenv("FILE_ID_NEQUI", "")    # file_id imagen nequi

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  # ID de la hoja de Google Sheets

# Conversación estados
(
    MONTO, CONFIRMAR_INVERSION, CODIGO_REFERIDO, CONFIRMAR_REGISTRO,
    NOMBRE, CEDULA, CONFIRMAR_DATOS, ESPERAR_COMPROBANTE, MENU_OPCIONES,
    ADMIN_BROADCAST_GET_MEDIA, ADMIN_BROADCAST_CONFIRM, ADMIN_REJECTION_REASON,
    NUEVA_INVERSION_MONTO, NUEVA_INVERSION_COMPROBANTE
) = range(14)

# Columnas que vamos a usar en la sheet (aseguramos que existan)
STANDARD_COLUMNS = [
    "Nombre", "Cédula", "Monto", "Referido", "CodigoUsuario",
    "ChatID", "FechaRegistro", "FechaPago", "ComprobanteFileID", "Estado", "AdminComentario", "NuevaInversion"
]

# Memoria en runtime
pending_rejects: Dict[int, int] = {}    # admin_id -> user_chat_id (esperando motivo)
pending_checks: Dict[int, float] = {}   # user_chat_id -> timestamp cuando se envió comprobante

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- GOOGLE SHEETS: conectar usando variable de entorno ----------------
def gsheet_client_from_env():
    """Crea y devuelve cliente gspread usando GOOGLE_SHEETS_CREDENTIALS variable."""
    creds_env = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_env:
        raise RuntimeError("Falta la variable de entorno GOOGLE_SHEETS_CREDENTIALS")
    # Si la variable es JSON en una linea, cargar
    creds_dict = json.loads(creds_env)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client

# Inicializar cliente y worksheet
try:
    gc = gsheet_client_from_env()
    sh = gc.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else None
    if sh:
        worksheet = sh.sheet1
    else:
        worksheet = None
except Exception as e:
    # Cuando deploy, si no hay creds o sheet id correcto, guardamos None.
    logger.warning("No se pudo inicializar Google Sheets: %s", e)
    worksheet = None

# Helper: leer hoja como DataFrame (si no existe crea con headers)
def read_sheet_df() -> pd.DataFrame:
    global worksheet, sh
    if worksheet is None:
        # intentar re-conectar (por si variable fue añadida después)
        try:
            client = gsheet_client_from_env()
            sh = client.open_by_key(SPREADSHEET_ID)
            worksheet = sh.sheet1
        except Exception as e:
            logger.error("Error reconectando Google Sheets: %s", e)
            # devolver dataframe vacío con columnas estándar
            return pd.DataFrame(columns=STANDARD_COLUMNS)

    try:
        records = worksheet.get_all_records()
        df = pd.DataFrame(records)
        # Asegurar columnas estándar
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[STANDARD_COLUMNS]
        return df
    except Exception as e:
        logger.error("Error leyendo sheet: %s", e)
        return pd.DataFrame(columns=STANDARD_COLUMNS)

def save_sheet_df(df: pd.DataFrame):
    """Reemplaza la sheet por el dataframe (cuidado: sobreescribe)."""
    global worksheet
    if worksheet is None:
        # intentar inicializar
        try:
            client = gsheet_client_from_env()
            sh_local = client.open_by_key(SPREADSHEET_ID)
            worksheet = sh_local.sheet1
        except Exception as e:
            logger.error("No se puede guardar en sheet, no está inicializada: %s", e)
            return

    # Reindex columns to STANDARD_COLUMNS and any additional ones
    # Combine to keep any user-added columns as well
    cols = STANDARD_COLUMNS.copy()
    for c in df.columns:
        if c not in cols:
            cols.append(c)
    df2 = df.reindex(columns=cols).fillna("")
    try:
        # Clear worksheet and write new header+values
        worksheet.clear()
        set_with_dataframe(worksheet, df2, include_index=False, include_column_header=True, resize=True)
    except Exception as e:
        logger.error("Error guardando df en sheet: %s", e)

# ---------------- Helpers varias ----------------
def format_money(n: int) -> str:
    return f"{n:,}".replace(",", ".")

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["Mis referidos", "Soporte"], ["Horarios de atención", "Nueva inversión"], ["Salir"]],
        one_time_keyboard=False,
        resize_keyboard=True
    )

def small_yesno_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("Sí")], [KeyboardButton("No")]], one_time_keyboard=True, resize_keyboard=True)

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Enviar imagen de montos y teclado
    amounts = [["200.000", "250.000"], ["300.000", "350.000"], ["400.000", "450.000"], ["500.000"]]
    keyboard = ReplyKeyboardMarkup(amounts, one_time_keyboard=True, resize_keyboard=True)

    try:
        if FILE_ID_MONTOS:
            await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_MONTOS)
    except Exception as e:
        logger.error("Error enviando imagen montos: %s", e)

    await update.message.reply_text(
        "💰 *Montos de inversión disponibles*\n\n"
        "Elige uno de los montos o escribe otro (usa puntos):",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return MONTO

# ---------------- Monto ----------------
async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        monto = int(text.replace(".", "").strip())
    except ValueError:
        await update.message.reply_text("❌ Ingresa un monto válido con puntos (ej: 200.000).")
        return MONTO

    if 200000 <= monto <= 500000:
        context.user_data["monto"] = monto
        ganancia = int(monto * 1.9)  # monto + 90%
        await update.message.reply_text(
            f"✅ Deseas invertir *{format_money(monto)}* COP?\n"
            f"En 10 días recibirás *{format_money(ganancia)}* COP.",
            parse_mode="Markdown",
            reply_markup=small_yesno_keyboard()
        )
        return CONFIRMAR_INVERSION
    else:
        await update.message.reply_text("⚠️ El monto debe estar entre 200.000 y 500.000 COP.")
        return MONTO

# ---------------- Confirmar inversion ----------------
async def confirmar_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        await update.message.reply_text("🔑 ¿Vienes referido por alguien? (Sí / No)", reply_markup=small_yesno_keyboard())
        return CODIGO_REFERIDO
    else:
        await update.message.reply_text("❌ Gracias por ingresar, vuelve cuando estés seguro.")
        return ConversationHandler.END

# ---------------- Recibir codigo referido ----------------
async def recibir_referido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    df = read_sheet_df()

    if text.lower() in ("no", "n"):
        context.user_data["referido"] = "Ninguno"
        await update.message.reply_text("📝 ¿Deseas continuar con el registro?", reply_markup=small_yesno_keyboard())
        return CONFIRMAR_REGISTRO

    # Si el usuario escribió algo, lo tratamos como codigo
    codigo = text.strip()
    # Buscar en la columna CodigoUsuario
    if "CodigoUsuario" in df.columns and not df.empty:
        # Comparar como string
        mask = df["CodigoUsuario"].astype(str) == str(codigo)
        if mask.any():
            # Tomar el ultimo nombre asociado
            nombre_ref = df[mask]["Nombre"].values[-1] if "Nombre" in df.columns else "(referente)"
            context.user_data["referido"] = codigo
            await update.message.reply_text(f"✅ Vienes referido por *{nombre_ref}* (código {codigo}).", parse_mode="Markdown")
            await update.message.reply_text("📝 ¿Deseas continuar con el registro?", reply_markup=small_yesno_keyboard())
            return CONFIRMAR_REGISTRO
        else:
            await update.message.reply_text("⚠️ Código no válido. Ingresa otro código o escribe 'No'.")
            return CODIGO_REFERIDO
    else:
        await update.message.reply_text("⚠️ No hay registros aún para validar códigos. Si tienes código, intenta más tarde o escribe 'No'.")
        return CODIGO_REFERIDO

# ---------------- Confirmar registro ----------------
async def confirmar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        await update.message.reply_text("✍️ Escribe tu *nombre completo*:", parse_mode="Markdown")
        return NOMBRE
    else:
        await update.message.reply_text("❌ Gracias, vuelve cuando estés seguro.")
        return ConversationHandler.END

# ---------------- Recibir nombre ----------------
async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text.strip()
    await update.message.reply_text("🆔 Ahora escribe tu *número de cédula*:", parse_mode="Markdown")
    return CEDULA

# ---------------- Recibir cedula ----------------
async def recibir_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cedula"] = update.message.text.strip()
    nombre = context.user_data.get("nombre", "")
    cedula = context.user_data.get("cedula", "")
    await update.message.reply_text(
        f"✅ Confirma tus datos:\n\n"
        f"👤 Nombre: {nombre}\n"
        f"🆔 Cédula: {cedula}\n\n"
        "¿Son correctos?",
        reply_markup=small_yesno_keyboard()
    )
    return CONFIRMAR_DATOS

# ---------------- Confirmar datos y registrar (sin asignar codigo aun) ----------------
async def confirmar_datos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("sí", "si"):
        nombre = context.user_data.get("nombre", "")
        cedula = context.user_data.get("cedula", "")
        referido = context.user_data.get("referido", "Ninguno")
        monto = context.user_data.get("monto", 0)
        chat_id = update.effective_chat.id

        df = read_sheet_df()
        ahora = datetime.now()
        fecha_pago = (ahora + timedelta(days=10)).strftime("%Y-%m-%d")

        nuevo = {
            "Nombre": nombre,
            "Cédula": cedula,
            "Monto": monto,
            "Referido": referido,
            "CodigoUsuario": "",  # se llenará al aprobar por admin
            "ChatID": chat_id,
            "FechaRegistro": ahora.strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago,
            "ComprobanteFileID": "",
            "Estado": "Esperando comprobante",
            "AdminComentario": "",
            "NuevaInversion": ""
        }

        df = pd.concat([df, pd.DataFrame([nuevo], columns=df.columns if not df.empty else STANDARD_COLUMNS)], ignore_index=True)
        # Asegurar que columnas estándar existan
        for c in STANDARD_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        save_sheet_df(df)

        # Mensaje y foto de cuenta
        await update.message.reply_text(
            f"🎉 Registro inicial exitoso {nombre}!\n\n"
            f"Envía el comprobante de tu pago a la siguiente cuenta y luego espera la validación.\n\n"
            f"Fecha estimada de pago: *{fecha_pago}* (10 días desde hoy).",
            parse_mode="Markdown"
        )
        try:
            if FILE_ID_NEQUI:
                await context.bot.send_photo(chat_id=chat_id, photo=FILE_ID_NEQUI)
        except Exception as e:
            logger.error("Error enviando imagen cuenta: %s", e)

        # Guardar timestamp para check posterior
        pending_checks[chat_id] = datetime.now().timestamp()
        # iniciar tarea de comprobación a 10 minutos
        asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))

        # ir a menu opciones
        await update.message.reply_text("¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
        return MENU_OPCIONES
    else:
        await update.message.reply_text("❌ Corrige tus datos. Escribe tu nombre completo:")
        return NOMBRE

# ---------------- Envío de comprobante por parte de usuario ----------------
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
    df = read_sheet_df()
    # localizar la última fila con este chat_id y estado 'Esperando comprobante' o 'Comprobante enviado'
    mask = (df["ChatID"] == chat_id) & (df["Estado"].str.contains("Esperando|Comprobante enviado", na=False))
    if not mask.any():
        # si no se encuentra, agregamos una fila mínima (por seguridad)
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
            "NuevaInversion": ""
        }
        df = pd.concat([df, pd.DataFrame([nuevo], columns=df.columns if not df.empty else STANDARD_COLUMNS)], ignore_index=True)
        save_sheet_df(df)
    else:
        idx = df[mask].index[-1]
        df.at[idx, "ComprobanteFileID"] = file_id
        df.at[idx, "Estado"] = "Comprobante enviado"
        save_sheet_df(df)

    # Preparar caption
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

    # Inline keyboard para admins: Aprobar / Rechazar
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

    await update.message.reply_text("⏳ Espera de 5 a 10 minutos mientras validamos tu transacción.")
    pending_checks[chat_id] = datetime.now().timestamp()
    asyncio.create_task(check_pending_after_delay(context, chat_id, delay_seconds=600))
    return MENU_OPCIONES

# ---------------- Verificar pendiente después de delay ----------------
async def check_pending_after_delay(context: ContextTypes.DEFAULT_TYPE, user_chat_id: int, delay_seconds: int = 600):
    await asyncio.sleep(delay_seconds)
    df = read_sheet_df()
    mask = (df["ChatID"] == user_chat_id) & (df["CodigoUsuario"] == "")
    if mask.any():
        try:
            await context.bot.send_message(chat_id=user_chat_id, text="¿Sigues ahí? Aún no hemos procesado tu comprobante. Si necesitas ayuda escribe 'Soporte'.")
            logger.info("Recordatorio enviado a %s", user_chat_id)
        except Exception as e:
            logger.error("Error enviando recordatorio a %s: %s", user_chat_id, e)
    pending_checks.pop(user_chat_id, None)

# ---------------- CALLBACK DE ADMINS (aprobar/rechazar) ----------------
async def validar_transaccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if "|" not in data:
        try:
            await query.edit_message_caption(caption="Comando inválido")
        except Exception:
            pass
        return

    accion, user_chat_id = data.split("|")
    user_chat_id = int(user_chat_id)
    admin_id = query.from_user.id

    df = read_sheet_df()
    mask = (df["ChatID"] == user_chat_id) & (df["Estado"].str.contains("Comprobante enviado|Esperando comprobante", na=False))
    if accion == "aprobar":
        # Generar código solo si no existe
        if mask.any():
            idx = df[mask].index[-1]
            if not df.at[idx, "CodigoUsuario"]:
                codigo = f"{random.randint(1000, 9999)}"
                df.at[idx, "CodigoUsuario"] = codigo
            else:
                codigo = df.at[idx, "CodigoUsuario"]
            monto = df.at[idx, "Monto"]
            fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
            df.at[idx, "Estado"] = "Aprobado"
            df.at[idx, "AdminComentario"] = f"Aprobado por admin {admin_id} el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            save_sheet_df(df)
        else:
            # si no hay fila, intentar crear una mínima
            codigo = f"{random.randint(1000, 9999)}"
            monto = context.user_data.get("monto", 0)
            fecha_pago = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")

        # Notificar al usuario
        try:
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=(
                    f"✅ Transacción aprobada!\n\n"
                    f"🔑 Tu código de usuario es: *INV-{codigo}*\n\n"
                    f"Has invertido: *{format_money(monto)}* COP\n"
                    f"Fecha estimada de pago: *{fecha_pago}*\n\n"
                    f"Recuerda: los referidos se consignan el mismo día a partir de las 7:00 PM (excepto domingos)."
                ),
                parse_mode="Markdown"
            )
            await context.bot.send_message(chat_id=user_chat_id, text="¿Deseas hacer algo más?", reply_markup=main_menu_keyboard())
        except Exception as e:
            logger.error("Error notificando aprobado a %s: %s", user_chat_id, e)

        # Notificar admin
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"Has aprobado la transacción de {user_chat_id}. Código: INV-{codigo}")
        except Exception:
            pass

        # limpiar pending
        pending_checks.pop(user_chat_id, None)

        # quitar inline buttons del mensaje del admin
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif accion == "rechazar":
        # Guardar que admin debe escribir motivo
        pending_rejects[admin_id] = user_chat_id
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"Escribe el *motivo* del rechazo para el usuario `{user_chat_id}`. Envia el texto ahora:", parse_mode="Markdown")
        except Exception as e:
            logger.error("Error pidiendo motivo al admin %s: %s", admin_id, e)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

# ---------------- Mensajes de admins que contienen motivo de rechazo ----------------
async def admin_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in pending_rejects:
        # no está en proceso de rechazo
        return

    user_chat_id = pending_rejects.pop(admin_id)
    motivo = update.message.text.strip()

    df = read_sheet_df()
    mask = (df["ChatID"] == user_chat_id) & (df["Estado"].str.contains("Comprobante enviado|Esperando comprobante", na=False))
    if mask.any():
        idx = df[mask].index[-1]
        df.at[idx, "Estado"] = "Rechazado"
        df.at[idx, "AdminComentario"] = f"Rechazado por admin {admin_id}: {motivo}"
        save_sheet_df(df)
    else:
        logger.warning("No se encontró fila para marcar rechazo de %s", user_chat_id)

    # Notificar usuario
    try:
        await context.bot.send_message(chat_id=user_chat_id, text=f"❌ Tu comprobante fue rechazado.\nMotivo: {motivo}\nPor favor revisa y vuelve a enviarlo.")
    except Exception as e:
        logger.error("Error notificando rechazo a %s: %s", user_chat_id, e)

    try:
        await context.bot.send_message(chat_id=admin_id, text=f"Motivo enviado y usuario notificado: {user_chat_id}")
    except Exception:
        pass

# ---------------- MENU OPCIONES (fijo) ----------------
async def menu_opciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    opcion = update.message.text.strip().lower()

    if opcion == "mis referidos":
        try:
            df = read_sheet_df()
            # buscar por CodigoUsuario del usuario
            # intentar obtener el codigo del usuario desde la hoja (buscamos por chatid)
            chat_id = update.effective_chat.id
            user_df = df[df["ChatID"] == chat_id]
            codigo = ""
            if not user_df.empty:
                # usar el ultimo registro del usuario para obtener su codigo
                codigo = str(user_df["CodigoUsuario"].values[-1]) if user_df["CodigoUsuario"].values[-1] else ""
            if not codigo:
                await update.message.reply_text("No tienes código asignado aún. Registra y espera aprobación.")
                return MENU_OPCIONES
            # ahora buscar referidos que tengan Referido == codigo
            referidos = df[df["Referido"].astype(str) == codigo]
            if referidos.empty:
                await update.message.reply_text("📋 No tienes referidos registrados.")
            else:
                lista = "\n".join([f"{r['Nombre']} - {r['Cédula']} (Monto: {format_money(int(r['Monto'])) if r['Monto'] else '0'})" for _, r in referidos.iterrows()])
                await update.message.reply_text(f"📋 Tus referidos:\n\n{lista}")
        except Exception as e:
            logger.error("Error consultando referidos: %s", e)
            await update.message.reply_text("⚠️ Error al consultar referidos.")
    elif opcion == "soporte":
        keyboard = ReplyKeyboardMarkup([["Volver al menú"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "📞 *Soporte*\n\n"
            "✉️ Correo: vortex440@gmail.com\n"
            "📱 WhatsApp 1: https://wa.link/oceivm\n"
            "📱 WhatsApp 2: https://wa.link/istt7e",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    elif opcion in ("horarios de atención", "horarios de atencion", "horarios"):
        keyboard = ReplyKeyboardMarkup([["Volver al menú"]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "🕑 *Horarios de Atención*\n\n"
            "📅 Lunes a Sábado: 8:00 AM - 7:00 PM\n"
            "📅 Domingo: 8:00 AM - 12:00 PM",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    elif opcion in ("sí", "si"):
        await update.message.reply_text("Perfecto, ¿qué deseas hacer?\n👉 Opciones: Mis referidos / Soporte / Horarios de atención / Nueva inversión / Salir")
    elif opcion == "no" or opcion == "salir":
        await update.message.reply_text("🙏 Gracias por confiar en nosotros. Nos vemos en 10 días con tu pago (o antes si tienes referidos).")
        return ConversationHandler.END
    elif opcion == "volver al menú":
        await start(update, context)
        return MONTO
    elif opcion == "nueva inversión" or opcion == "nueva inversion":
        return await nueva_inversion(update, context)
    else:
        # mostrar teclado con opciones fijas
        await update.message.reply_text("👉 Elige una opción:", reply_markup=main_menu_keyboard())

    return MENU_OPCIONES

# ---------------- ADMIN: broadcast (texto + imagen) ----------------
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
    df = read_sheet_df()
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
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error("Error en broadcast a %s: %s", cid, e)
            failed += 1

    await update.message.reply_text(f"Broadcast enviado. Exitosos: {sent}, fallidos: {failed}")
    return ConversationHandler.END

# ---------------- NUEVA INVERSION ----------------
async def nueva_inversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # reenviar imagen de montos y teclado
    amounts = [["200.000", "250.000"], ["300.000", "350.000"], ["400.000", "450.000"], ["500.000"]]
    keyboard = ReplyKeyboardMarkup(amounts, one_time_keyboard=True, resize_keyboard=True)
    try:
        if FILE_ID_MONTOS:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=FILE_ID_MONTOS)
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
    chat_id = update.effective_chat.id
    # obtener file id
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

    monto = context.user_data.get("nueva_monto", 0)
    fecha_pago = context.user_data.get("nueva_fecha_pago", datetime.now() + timedelta(days=10))

    df = read_sheet_df()
    mask = df["ChatID"] == chat_id
    if mask.any():
        idx = df[mask].index[-1]
        # crear nuevas columnas si es necesario
        # Añadimos registro de nueva inversión manteniendo fila base:
        # Aquí agregamos una fila nueva detallando la nueva inversión (historia)
        nuevo = {
            "Nombre": df.at[idx, "Nombre"] if "Nombre" in df.columns else "",
            "Cédula": df.at[idx, "Cédula"] if "Cédula" in df.columns else "",
            "Monto": monto,
            "Referido": df.at[idx, "Referido"] if "Referido" in df.columns else "",
            "CodigoUsuario": df.at[idx, "CodigoUsuario"] if "CodigoUsuario" in df.columns else "",
            "ChatID": chat_id,
            "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago.strftime("%Y-%m-%d"),
            "ComprobanteFileID": file_id,
            "Estado": "Comprobante enviado",
            "AdminComentario": "",
            "NuevaInversion": monto
        }
        df = pd.concat([df, pd.DataFrame([nuevo], columns=df.columns if not df.empty else STANDARD_COLUMNS)], ignore_index=True)
        save_sheet_df(df)
    else:
        # usuario no registrado previamente -> crear fila mínima
        nuevo = {
            "Nombre": context.user_data.get("nombre", ""),
            "Cédula": context.user_data.get("cedula", ""),
            "Monto": monto,
            "Referido": context.user_data.get("referido", "Ninguno"),
            "CodigoUsuario": context.user_data.get("codigo_usuario", ""),
            "ChatID": chat_id,
            "FechaRegistro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "FechaPago": fecha_pago.strftime("%Y-%m-%d"),
            "ComprobanteFileID": file_id,
            "Estado": "Comprobante enviado",
            "AdminComentario": "",
            "NuevaInversion": monto
        }
        df = pd.concat([df, pd.DataFrame([nuevo], columns=df.columns if not df.empty else STANDARD_COLUMNS)], ignore_index=True)
        save_sheet_df(df)

    # Notificar admins
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

# ---------------- MAIN / Handlers ----------------
def main():
    if not TOKEN:
        logger.error("Falta la variable de entorno TELEGRAM_TOKEN. Abortando.")
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
            ESPERAR_COMPROBANTE: [MessageHandler(filters.PHOTO | filters.Document.ALL & ~filters.COMMAND, recibir_comprobante)],
                        MENU_OPCIONES: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_opciones)],

            # Admin broadcast
            ADMIN_BROADCAST_GET_MEDIA: [MessageHandler(
                (filters.PHOTO | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND,
                admin_broadcast_receive_media
            )],
            ADMIN_BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_confirm)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv_handler)

    # Handler para callbacks de admins
    app.add_handler(CallbackQueryHandler(validar_transaccion))

    # Handler para motivo de rechazo
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reason_handler))

    # Comando broadcast (solo admins)
    app.add_handler(CommandHandler("broadcast", admin_broadcast_command))

    app.run_polling()


if __name__ == "__main__":
    main()




