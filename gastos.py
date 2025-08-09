import os
import re
import sqlite3
import json
import io
import csv
import random
import logging
import matplotlib.pyplot as plt
from datetime import datetime, time, timezone, timedelta
from dateutil.relativedelta import relativedelta
import pytz
from thefuzz import process, fuzz
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# --- Configuração dos Logs ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Estados das Conversas ---
ESCOLHER_PERIODO, AGUARDANDO_DATA_INICIO, AGUARDANDO_DATA_FIM = range(3)
AGUARDANDO_PAGAMENTO, AGUARDANDO_SUGESTAO_CATEGORIA = range(10, 12)
ONBOARDING_INICIO, ONBOARDING_ORCAMENTO, ONBOARDING_TRANSACAO = range(20, 23)

# --- Configuração da Base de Dados ---
DATA_DIR = '/data'
DB_PATH = os.path.join(DATA_DIR, "gastos_bot.db")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

def inicializar_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE, chat_id INTEGER, nome_usuario TEXT, data_criacao TEXT, ultimo_lancamento TEXT, dias_sequencia INTEGER DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS categorias (id INTEGER PRIMARY KEY, id_usuario INTEGER, nome TEXT, UNIQUE(id_usuario, nome), FOREIGN KEY (id_usuario) REFERENCES usuarios (id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS cartoes (id INTEGER PRIMARY KEY AUTOINCREMENT, id_usuario INTEGER, nome TEXT, limite REAL, dia_fechamento INTEGER, UNIQUE(id_usuario, nome), FOREIGN KEY (id_usuario) REFERENCES usuarios(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS transacoes (id INTEGER PRIMARY KEY, id_usuario INTEGER, id_categoria INTEGER, valor REAL, tipo TEXT, data_transacao TEXT, id_cartao INTEGER, FOREIGN KEY (id_usuario) REFERENCES usuarios (id), FOREIGN KEY (id_categoria) REFERENCES categorias (id), FOREIGN KEY (id_cartao) REFERENCES cartoes(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS lembretes_diarios (id_usuario INTEGER PRIMARY KEY, horario TEXT, chat_id INTEGER, FOREIGN KEY (id_usuario) REFERENCES usuarios(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS agendamentos (id INTEGER PRIMARY KEY AUTOINCREMENT, id_usuario INTEGER, dia INTEGER, horario TEXT, titulo TEXT, valor REAL, chat_id INTEGER, UNIQUE(id_usuario, titulo), FOREIGN KEY (id_usuario) REFERENCES usuarios(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS orcamentos (id INTEGER PRIMARY KEY AUTOINCREMENT, id_usuario INTEGER, id_categoria INTEGER, valor REAL, UNIQUE(id_usuario, id_categoria), FOREIGN KEY (id_usuario) REFERENCES usuarios(id), FOREIGN KEY (id_categoria) REFERENCES categorias(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS assinaturas (id_usuario INTEGER PRIMARY KEY, plano TEXT, data_expiracao TEXT, FOREIGN KEY (id_usuario) REFERENCES usuarios(id))')
    conn.commit()
    conn.close()

# --- Decorador de Acesso Premium ---
def acesso_premium_necessario(func):
    """Um decorador que verifica se o usuário tem uma assinatura ativa."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id_telegram = update.effective_user.id
        user_id_interno = get_user_id(user_id_telegram)

        if not user_id_interno:
            await update.effective_message.reply_text("Por favor, inicie o bot com /start primeiro.")
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT data_expiracao FROM assinaturas WHERE id_usuario = ?", (user_id_interno,))
        assinatura = cursor.fetchone()
        conn.close()

        if assinatura and datetime.strptime(assinatura[0], '%Y-%m-%d') >= datetime.now():
            return await func(update, context, *args, **kwargs)
        else:
            texto_venda = "💎 Esta é uma funcionalidade exclusiva para assinantes Premium! Faça o upgrade para ter acesso a orçamentos, insights e muito mais."
            await update.effective_message.reply_text(texto_venda)
            return
    return wrapper

# --- Funções Auxiliares ---
def get_user_id(telegram_id):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM usuarios WHERE telegram_id = ?", (telegram_id,)); user = cursor.fetchone(); conn.close()
    return user[0] if user else None

def gerar_grafico_pizza(gastos_por_categoria):
    if not gastos_por_categoria: return None
    labels = [item[0].capitalize() for item in gastos_por_categoria]; sizes = [item[1] for item in gastos_por_categoria]
    fig, ax = plt.subplots(figsize=(8, 6)); ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140, pctdistance=0.85)
    centre_circle = plt.Circle((0,0),0.70,fc='white'); fig.gca().add_artist(centre_circle); ax.axis('equal')
    plt.title('Distribuição de Gastos do Período', pad=20); buf = io.BytesIO(); plt.savefig(buf, format='png', bbox_inches='tight'); plt.close(fig); buf.seek(0)
    return buf

# --- Comandos Principais e Onboarding ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    chat_id = update.effective_chat.id
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, dias_sequencia FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    user_data = cursor.fetchone()

    if not user_data:
        data_criacao_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO usuarios (telegram_id, chat_id, nome_usuario, data_criacao, dias_sequencia) VALUES (?, ?, ?, ?, ?)", 
                       (telegram_id, chat_id, user.username, data_criacao_str, 0))
        conn.commit()
        conn.close()

        welcome_text = (f"Olá, {user.first_name}! 👋 Seja muito bem-vindo(a) ao PlinBot!\n\n"
                        "Vejo que é sua primeira vez por aqui. Gostaria de um tour rápido para aprender a usar as principais funções?")
        
        keyboard = [[
            InlineKeyboardButton("Sim, vamos lá! 🚀", callback_data="onboarding_start"),
            InlineKeyboardButton("Não, obrigado.", callback_data="onboarding_skip_all")
        ]]
        await update.effective_message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return ONBOARDING_INICIO
    else:
        user_id_local, dias_sequencia = user_data
        cursor.execute("UPDATE usuarios SET chat_id = ? WHERE telegram_id = ?", (chat_id, telegram_id))
        conn.commit()
        conn.close()
        
        reply_keyboard = [
            ["📊 Relatório", "💳 Cartões"], 
            ["🗂️ Categorias", "💡 Ajuda"], 
            ["⏰ Lembretes/Agendamentos", "⬇️ Exportar"],
            ["🏠 Menu Principal"]
        ]
        markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
        
        agora_utc = datetime.now(timezone.utc)
        inicio_mes_str = agora_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT SUM(valor) FROM transacoes WHERE id_usuario = ? AND tipo = 'saida' AND data_transacao >= ?", (user_id_local, inicio_mes_str))
        gastos_mes = cursor.fetchone()[0] or 0.0
        conn.close()
        
        nome = user.first_name
        mensagem = f"Olá de volta, {nome}!\n\n"
        mensagem += f"📊 Até agora, seus gastos este mês somam *R$ {gastos_mes:.2f}*.\n\n"
        
        if dias_sequencia > 1:
            mensagem += f"Você está em uma sequência de *{dias_sequencia} dias* registrando tudo! Continue assim! 🔥"
        else:
            mensagem += "O que vamos organizar hoje?"

        await update.effective_message.reply_text(mensagem, reply_markup=markup, parse_mode='Markdown')
        return ConversationHandler.END

async def onboarding_iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['onboarding'] = True
    texto = ("Ótimo! Primeiro, vamos cadastrar um cartão. Isso ajuda a organizar os gastos.\n\n"
             "Use o comando `/add_cartao <Nome> <Limite> <Dia do Fechamento>`.\n\n"
             "*Exemplo:* `/add_cartao Nubank 1500 28`")
    keyboard = [[InlineKeyboardButton("Pular este passo ➡️", callback_data="onboarding_skip_card")]]
    await query.edit_message_text(text=texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return ONBOARDING_ORCAMENTO

async def onboarding_pular_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Agora ele vai direto pedir para registrar a transação
    return await onboarding_pedir_transacao(update, context)

async def onboarding_pedir_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ("Excelente! Agora, vamos definir um orçamento. Isso te ajuda a não gastar mais do que o planejado.\n\n"
             "Use o comando `/orcamento <Categoria> <Valor>`\n\n"
             "*Exemplo:* `/orcamento Lazer 500`")
    keyboard = [[InlineKeyboardButton("Pular este passo ➡️", callback_data="onboarding_skip_budget")]]
    if update.callback_query:
        await update.callback_query.edit_message_text(text=texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text=texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return ONBOARDING_TRANSACAO

async def onboarding_pular_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await onboarding_pedir_transacao(update, context)
    
async def onboarding_pedir_transacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ("Perfeito! Você está quase pronto(a).\n\n"
             "A principal função do bot é registrar suas transações. Tente registrar seu último gasto agora mesmo!\n\n"
             "Use o formato: `-<valor> <categoria>`\n\n"
             "*Exemplo:* `-15 almoço`")
    keyboard = [[InlineKeyboardButton("Finalizar Tour ✅", callback_data="onboarding_skip_all")]]
    if update.callback_query:
        await update.callback_query.edit_message_text(text=texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text=texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return ONBOARDING_TRANSACAO

async def onboarding_finalizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Se a função foi chamada por um botão (query existe)
    if query:
        await query.answer()
        
    if 'onboarding' in context.user_data:
        del context.user_data['onboarding']
        
    texto = ("Prontinho! Você aprendeu o básico. Agora o bot é todo seu.\n\n"
             "Lembre-se que você pode usar os botões do menu a qualquer momento para acessar as funções.")
    
    # Responde à mensagem original ou edita a mensagem do botão
    target_message = query.message if query else update.effective_message
    await target_message.reply_text(text=texto)
    
    # Chama o start para mostrar o menu principal
    await start(update, context)
    return ConversationHandler.END

async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_ajuda = (
        "🤖 *Comandos e Funções*\n\n"
        "Para registrar uma transação:\n"
        "`-valor categoria` (gastos)\n"
        "`+valor categoria` (receitas)\n\n"
        "💰 *Orçamentos (Premium):*\n"
        "  `/orcamento <categoria> <valor>`\n"
        "  `/meus_orcamentos`\n"
        "  `/del_orcamento <categoria>`\n\n"
        "💳 *Cartões de Crédito:*\n"
        "  `/add_cartao <nome> <limite> <dia_fecha>`\n"
        "  `/list_cartoes`\n"
        "  `/fatura <nome_cartao>`\n"
        "  `/del_cartao <nome>`\n\n"
        "⏰ *Lembretes e Agendamentos (Premium):*\n"
        "  `/agendar <dia> <HH:MM> [valor] <título>`\n"
        "  `/ver_agendamentos`\n"
        "  `/cancelar_agendamento <título>`\n\n"
        "📊 *Análise e Exportação (Premium):*\n"
        "  `/exportar`"
    )
    await update.effective_message.reply_text(texto_ajuda, parse_mode='Markdown')

# (O resto do código segue abaixo)
async def finalizar_onboarding_com_transacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa a transação final do onboarding e encerra o tutorial."""
    texto = update.effective_message.text
    padrao = re.compile(r'^([+\-])\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)$')
    match = padrao.match(texto)

    if not match:
        await update.effective_message.reply_text("Formato inválido. Tente algo como `-15 almoço` ou clique para finalizar o tour.")
        return ONBOARDING_TRANSACAO # Permanece no mesmo estado se errar

    # Registra a transação de forma simplificada, sem pedir forma de pagamento
    user_id = get_user_id(update.effective_user.id)
    sinal, valor_str, nome_categoria = match.groups()
    nome_categoria = nome_categoria.strip().lower()
    
    if not nome_categoria:
        await update.effective_message.reply_text("Você esqueceu da categoria! Tente `-15 almoço`.")
        return ONBOARDING_TRANSACAO

    # Chama a sua função principal de registro, mas sem o 'update' para não responder duas vezes
    # e sem pedir cartão (id_cartao=None)
    await registrar_transacao_final(update=None, context=context, user_id=user_id, nome_categoria=nome_categoria, sinal=sinal, valor_str=valor_str, id_cartao=None)
    
    # Mensagem de sucesso e finalização do tour
    await update.effective_message.reply_text("Perfeito, sua primeira transação foi registrada!")
    
    # Chama a função que realmente finaliza e mostra o menu principal
    return await onboarding_finalizar(update, context)
# (Continuando o código...)
async def add_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT data_expiracao FROM assinaturas WHERE id_usuario = ?", (user_id,))
    assinatura = cursor.fetchone()
    is_premium = bool(assinatura and datetime.strptime(assinatura[0], '%Y-%m-%d') >= datetime.now())

    if not is_premium:
        cursor.execute("SELECT COUNT(id) FROM cartoes WHERE id_usuario = ?", (user_id,))
        num_cartoes = cursor.fetchone()[0]
        if num_cartoes >= 1:
            # <<< A MUDANÇA ESTÁ AQUI >>>
            await handle_premium_upsell(update, context, feature_name="1 cartão de crédito")
            conn.close()
            return

    try:
        nome_cartao = context.args[0].capitalize(); limite = float(context.args[1].replace(',', '.')); dia_fechamento = int(context.args[2])
        if not (1 <= dia_fechamento <= 31 and limite > 0): raise ValueError()
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Formato inválido! Use: `/add_cartao <nome> <limite> <dia_fecha>`")
        conn.close()
        return
    
    try:
        cursor.execute("INSERT INTO cartoes (id_usuario, nome, limite, dia_fechamento) VALUES (?, ?, ?, ?)", (user_id, nome_cartao, limite, dia_fechamento))
        conn.commit()
        await update.effective_message.reply_text(f"💳 Cartão '{nome_cartao}' adicionado!")
        if context.user_data.get('onboarding'):
            conn.close()
            return await onboarding_pedir_transacao(update, context)
    except sqlite3.IntegrityError:
        await update.effective_message.reply_text(f"⚠️ Já existe um cartão com o nome '{nome_cartao}'.")
    finally:
        conn.close()

@acesso_premium_necessario
async def set_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    try:
        args = context.args
        valor = float(args[-1].replace(',', '.'))
        nome_categoria = " ".join(args[:-1]).lower()
        if not nome_categoria or valor <= 0: raise ValueError()
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT id FROM categorias WHERE id_usuario = ? AND nome = ?", (user_id, nome_categoria))
        categoria = cursor.fetchone()
        if not categoria:
            cursor.execute("INSERT INTO categorias (id_usuario, nome) VALUES (?, ?)", (user_id, nome_categoria)); conn.commit()
            categoria_id = cursor.lastrowid
        else:
            categoria_id = categoria[0]
        cursor.execute("REPLACE INTO orcamentos (id_usuario, id_categoria, valor) VALUES (?, ?, ?)", (user_id, categoria_id, valor)); conn.commit(); conn.close()
        await update.effective_message.reply_text(f"✅ Orçamento de R$ {valor:.2f} definido para a categoria '{nome_categoria.capitalize()}'.")
        if context.user_data.get('onboarding'):
            return await onboarding_pedir_transacao(update, context)
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Formato inválido! Use: `/orcamento <categoria> <valor>`\nExemplo: `/orcamento lazer 300`")

@acesso_premium_necessario
async def list_orcamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    agora_utc = datetime.now(timezone.utc); inicio_mes_str = agora_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    query = "SELECT c.nome, o.valor, (SELECT SUM(t.valor) FROM transacoes t WHERE t.id_categoria = c.id AND t.id_usuario = ? AND t.tipo = 'saida' AND t.data_transacao >= ?) as gasto_total FROM orcamentos o JOIN categorias c ON o.id_categoria = c.id WHERE o.id_usuario = ? ORDER BY c.nome"
    cursor.execute(query, (user_id, inicio_mes_str, user_id)); orcamentos = cursor.fetchall(); conn.close()
    if not orcamentos:
        await update.effective_message.reply_text("Você ainda não definiu nenhum orçamento. Use `/orcamento <categoria> <valor>` para começar.")
        return
    resposta = ["💰 *Seus Orçamentos para este Mês:*\n"]
    for nome_cat, valor_orc, gasto_total in orcamentos:
        gasto_total = gasto_total or 0.0
        percentual = (gasto_total / valor_orc) * 100 if valor_orc > 0 else 0
        resposta.append(f"🔹 *{nome_cat.capitalize()}*: R$ {gasto_total:.2f} de R$ {valor_orc:.2f} ({percentual:.1f}%)")
    await update.effective_message.reply_text("\n".join(resposta), parse_mode='Markdown')

@acesso_premium_necessario
async def del_orcamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    try:
        nome_categoria = " ".join(context.args).lower()
        if not nome_categoria: raise ValueError()
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT id FROM categorias WHERE id_usuario = ? AND nome = ?", (user_id, nome_categoria)); categoria = cursor.fetchone()
        if not categoria:
            await update.effective_message.reply_text(f"Não encontrei a categoria '{nome_categoria.capitalize()}'."); conn.close()
            return
        categoria_id = categoria[0]
        cursor.execute("DELETE FROM orcamentos WHERE id_usuario = ? AND id_categoria = ?", (user_id, categoria_id))
        if cursor.rowcount > 0:
            conn.commit()
            await update.effective_message.reply_text(f"✅ Orçamento para '{nome_categoria.capitalize()}' removido.")
        else:
            await update.effective_message.reply_text(f"Você não tinha um orçamento definido para '{nome_categoria.capitalize()}'.")
        conn.close()
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Formato inválido! Use: `/del_orcamento <categoria>`")

# ... (todas as outras funções do seu bot, como menu_lembretes_e_agendamentos, etc., vêm aqui) ...
async def menu_lembretes_e_agendamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ("Aqui pode configurar suas notificações:\n\n"
             "☀️ *LEMBRETE DIÁRIO* (para registrar gastos)\n`/lembrete HH:MM`\n`/cancelar_lembrete`\n\n"
             "🗓️ *AGENDADOR DE CONTAS* (registo automático)\n`/agendar <dia> <HH:MM> [valor] <título>`\n`/ver_agendamentos`\n`/cancelar_agendamento <título>`")
    await update.effective_message.reply_text(texto, parse_mode='Markdown')

async def menu_cartoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ("Aqui pode gerir os seus cartões de crédito:\n\n"
             "➡️ Para adicionar:\n`/add_cartao <Nome> <Limite> <Dia do Fechamento>`\n*Exemplo:* `/add_cartao Nubank 1500 28`\n\n"
             "➡️ Para consultar:\n`/list_cartoes`\n`/fatura <Nome do Cartão>`\n\n"
             "➡️ Para remover:\n`/del_cartao <Nome do Cartão>`")
    await update.effective_message.reply_text(texto, parse_mode='Markdown')

async def list_cartoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, nome, limite, dia_fechamento FROM cartoes WHERE id_usuario = ? ORDER BY nome", (user_id,)); cartoes = cursor.fetchall()
    if not cartoes: await update.effective_message.reply_text("Nenhum cartão adicionado. Use `/add_cartao`."); conn.close(); return
    resposta = ["💳 *Sua Carteira de Cartões:*\n"]
    for id_cartao, nome, limite, dia_fechamento in cartoes:
        fatura_atual, _, _ = calcular_fatura(id_cartao, dia_fechamento); limite_disponivel = limite - fatura_atual
        resposta.append(f"Card: *{nome}* (Fecha dia {dia_fechamento})"); resposta.append(f"Fatura Aberta: R$ {fatura_atual:.2f}"); resposta.append(f"Limite Disponível: R$ {limite_disponivel:.2f}\n")
    conn.close(); await update.effective_message.reply_text("\n".join(resposta), parse_mode='Markdown')

def calcular_fatura(id_cartao, dia_fechamento):
    hoje = datetime.now(pytz.timezone('America/Sao_Paulo'))
    if hoje.day > dia_fechamento: data_fim_fatura = (hoje + relativedelta(months=1)).replace(day=dia_fechamento)
    else: data_fim_fatura = hoje.replace(day=dia_fechamento)
    data_inicio_fatura = (data_fim_fatura - relativedelta(months=1)) + timedelta(days=1)
    inicio_str = data_inicio_fatura.replace(hour=0, minute=0, second=0).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    fim_str = data_fim_fatura.replace(hour=23, minute=59, second=59).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT SUM(valor) FROM transacoes WHERE id_cartao = ? AND tipo = 'saida' AND data_transacao BETWEEN ? AND ?", (id_cartao, inicio_str, fim_str)); fatura_total = cursor.fetchone()[0] or 0.0; conn.close()
    return fatura_total, data_inicio_fatura, data_fim_fatura

async def fatura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    try: nome_cartao = " ".join(context.args).capitalize();
    except IndexError: await update.effective_message.reply_text("Uso: `/fatura <nome do cartão>`"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, limite, dia_fechamento FROM cartoes WHERE id_usuario = ? AND nome = ?", (user_id, nome_cartao)); cartao = cursor.fetchone()
    if not cartao: await update.effective_message.reply_text(f"Não encontrei o cartão '{nome_cartao}'."); conn.close(); return
    id_cartao, limite, dia_fechamento = cartao
    fatura_total, data_inicio, data_fim = calcular_fatura(id_cartao, dia_fechamento); limite_disponivel = limite - fatura_total
    resposta = [f"📊 *Fatura Aberta - {nome_cartao}*", f"Período: {data_inicio.strftime('%d/%m')} a {data_fim.strftime('%d/%m')}\n", f"Total da Fatura: *R$ {fatura_total:.2f}*", f"Limite Disponível: R$ {limite_disponivel:.2f}\n"]
    inicio_str = data_inicio.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'); fim_str = data_fim.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("SELECT valor, c.nome FROM transacoes t JOIN categorias c ON t.id_categoria = c.id WHERE t.id_cartao = ? AND t.data_transacao BETWEEN ? AND ? ORDER BY t.data_transacao DESC LIMIT 5", (id_cartao, inicio_str, fim_str)); ultimos_gastos = cursor.fetchall(); conn.close()
    if ultimos_gastos:
        resposta.append("*Últimos Lançamentos:*")
        for valor, categoria in ultimos_gastos: resposta.append(f"- {categoria.capitalize()}: R$ {valor:.2f}")
    await update.effective_message.reply_text("\n".join(resposta), parse_mode='Markdown')

async def del_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    try: nome_cartao = " ".join(context.args).capitalize()
    except IndexError: await update.effective_message.reply_text("Uso: `/del_cartao <nome>`"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM cartoes WHERE id_usuario = ? AND nome = ?", (user_id, nome_cartao)); cartao = cursor.fetchone()
    if not cartao: await update.effective_message.reply_text(f"Não encontrei o cartão '{nome_cartao}'."); conn.close(); return
    cartao_id = cartao[0]; cursor.execute("UPDATE transacoes SET id_cartao = NULL WHERE id_cartao = ?", (cartao_id,)); cursor.execute("DELETE FROM cartoes WHERE id = ?", (cartao_id,)); conn.commit(); conn.close()
    await update.effective_message.reply_text(f"✅ Cartão '{nome_cartao}' removido.")

async def list_categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT nome FROM categorias WHERE id_usuario = ? ORDER BY nome", (user_id,)); categorias = cursor.fetchall(); conn.close()
    if not categorias: await update.effective_message.reply_text("Você ainda não tem categorias."); return
    lista_formatada = ["*Suas Categorias:*\n"] + [f"- {nome.capitalize()}" for nome, in categorias]
    await update.effective_message.reply_text("\n".join(lista_formatada), parse_mode='Markdown')

async def del_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id)
    try: nome_categoria = context.args[0].lower()
    except IndexError: await update.effective_message.reply_text("Uso: `/del_categoria <nome>`"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM categorias WHERE id_usuario = ? AND nome = ?", (user_id, nome_categoria)); categoria = cursor.fetchone()
    if not categoria: await update.effective_message.reply_text(f"Categoria '{nome_categoria}' não encontrada."); conn.close(); return
    categoria_id = categoria[0]; cursor.execute("UPDATE transacoes SET id_categoria = NULL WHERE id_categoria = ?", (categoria_id,)); cursor.execute("DELETE FROM categorias WHERE id = ?", (categoria_id,)); conn.commit(); conn.close()
    await update.effective_message.reply_text(f"✅ Categoria '{nome_categoria}' apagada.")

ESCOLHER_PERIODO, AGUARDANDO_DATA_INICIO, AGUARDANDO_DATA_FIM = range(3)
async def iniciar_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Mês Atual", callback_data="rel_mes_atual")], [InlineKeyboardButton("Mês Anterior", callback_data="rel_mes_anterior")], [InlineKeyboardButton("Período Específico", callback_data="rel_periodo_especifico")]]
    await update.effective_message.reply_text("Qual período gostaria de analisar?", reply_markup=InlineKeyboardMarkup(keyboard)); return ESCOLHER_PERIODO
async def processar_escolha_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); escolha = query.data; agora = datetime.now(timezone.utc)
    if escolha == "rel_mes_atual":
        await query.edit_message_text("Gerando relatório do mês atual..."); inicio = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0); fim = (inicio + relativedelta(months=1)) - timedelta(seconds=1)
        return await gerar_relatorio(update, context, inicio, fim)
    elif escolha == "rel_mes_anterior":
        await query.edit_message_text("Gerando relatório do mês anterior..."); primeiro_dia_mes_atual = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0); ultimo_dia_mes_anterior = primeiro_dia_mes_atual - timedelta(days=1); inicio = ultimo_dia_mes_anterior.replace(day=1, hour=0, minute=0, second=0, microsecond=0); fim = primeiro_dia_mes_atual - timedelta(seconds=1)
        return await gerar_relatorio(update, context, inicio, fim)
    elif escolha == "rel_periodo_especifico":
        await query.edit_message_text("Ok. Por favor, envie-me a *data de início* no formato `DD/MM/AAAA`.", parse_mode='Markdown'); return AGUARDANDO_DATA_INICIO
async def receber_data_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data_inicio = datetime.strptime(update.effective_message.text, '%d/%m/%Y'); context.user_data['data_inicio_relatorio'] = data_inicio
        await update.effective_message.reply_text("Ótimo. Agora, envie-me a *data de fim* (`DD/MM/AAAA`).", parse_mode='Markdown'); return AGUARDANDO_DATA_FIM
    except ValueError: await update.effective_message.reply_text("Formato de data inválido. Use `DD/MM/AAAA`. Tente novamente ou /cancelar."); return AGUARDANDO_DATA_INICIO
async def receber_data_fim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data_inicio = context.user_data['data_inicio_relatorio']; data_fim = datetime.strptime(update.effective_message.text, '%d/%m/%Y'); data_fim = data_fim.replace(hour=23, minute=59, second=59)
        if data_inicio > data_fim: await update.effective_message.reply_text("A data de fim não pode ser anterior à de início. Envie a data de fim novamente."); return AGUARDANDO_DATA_FIM
        await update.effective_message.reply_text("Certo! Gerando o seu relatório personalizado...")
        del context.user_data['data_inicio_relatorio']
        fuso_local = pytz.timezone('America/Sao_Paulo'); inicio_local = fuso_local.localize(data_inicio); fim_local = fuso_local.localize(data_fim)
        return await gerar_relatorio(update, context, inicio_local.astimezone(timezone.utc), fim_local.astimezone(timezone.utc))
    except (ValueError, KeyError): await update.effective_message.reply_text("Ocorreu um erro. Use `DD/MM/AAAA` ou /cancelar para recomeçar."); return AGUARDANDO_DATA_FIM
async def gerar_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE, data_inicio, data_fim):
    user_id_telegram = update.effective_user.id
    user_id_interno = get_user_id(user_id_telegram)
    
    inicio_str = data_inicio.strftime('%Y-%m-%d %H:%M:%S')
    fim_str = data_fim.strftime('%Y-%m-%d %H:%M:%S')
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # ### MUDANÇA ###: Verifica se o usuário é premium
    cursor.execute("SELECT data_expiracao FROM assinaturas WHERE id_usuario = ?", (user_id_interno,))
    assinatura = cursor.fetchone()
    is_premium = bool(assinatura and datetime.strptime(assinatura[0], '%Y-%m-%d') >= datetime.now())
    # ### FIM DA MUDANÇA ###
    
    cursor.execute("SELECT SUM(valor) FROM transacoes WHERE id_usuario = ? AND tipo = 'entrada' AND data_transacao BETWEEN ? AND ?", (user_id_interno, inicio_str, fim_str)); entradas = cursor.fetchone()[0] or 0.0
    cursor.execute("SELECT SUM(valor) FROM transacoes WHERE id_usuario = ? AND tipo = 'saida' AND data_transacao BETWEEN ? AND ?", (user_id_interno, inicio_str, fim_str)); saidas = cursor.fetchone()[0] or 0.0
    saldo = entradas - saidas
    cursor.execute("SELECT c.nome, SUM(t.valor) FROM transacoes t LEFT JOIN categorias c ON t.id_categoria = c.id WHERE t.id_usuario = ? AND t.tipo = 'saida' AND data_transacao BETWEEN ? AND ? GROUP BY c.nome ORDER BY SUM(t.valor) DESC", (user_id_interno, inicio_str, fim_str)); gastos_por_categoria = cursor.fetchall()
    conn.close()
    
    titulo_periodo = f"de {data_inicio.astimezone(pytz.timezone('America/Sao_Paulo')).strftime('%d/%m/%Y')} a {data_fim.astimezone(pytz.timezone('America/Sao_Paulo')).strftime('%d/%m/%Y')}"
    legenda_texto = [f"📊 *Relatório do Período*\n_{titulo_periodo}_", f"🟢 Entradas: R$ {entradas:.2f}", f"🔴 Saídas: R$ {saidas:.2f}", f"💰 Saldo do Período: R$ {saldo:.2f}"]
    
    if gastos_por_categoria:
        legenda_texto.append("\n*Gastos por Categoria:*")
        for nome, total in gastos_por_categoria:
            percentual = (total / saidas) * 100 if saidas > 0 else 0
            legenda_texto.append(f"  - {nome.capitalize()}: R$ {total:.2f} ({percentual:.1f}%)")
            
    # ### MUDANÇA ###: Só gera o gráfico se for premium
    buffer_imagem = None
    if is_premium:
        buffer_imagem = gerar_grafico_pizza(gastos_por_categoria)
    # ### FIM DA MUDANÇA ###
    
    mensagem_final = "\n".join(legenda_texto)
    
    if update.callback_query: await update.callback_query.delete_message()
    
    if buffer_imagem:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=buffer_imagem, caption=mensagem_final, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=mensagem_final, parse_mode='Markdown')
        
    return ConversationHandler.END
async def cancelar_conversa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ['data_inicio_relatorio', 'transacao_pendente', 'sugestao_categoria']:
        if key in context.user_data: del context.user_data[key]
    await update.effective_message.reply_text("Operação cancelada."); return ConversationHandler.END

@acesso_premium_necessario
async def exportar_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); agora_utc = datetime.now(timezone.utc); inicio_mes_utc_str = agora_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S'); conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT t.data_transacao, t.tipo, t.valor, c.nome as cat_nome, cart.nome as cart_nome FROM transacoes t LEFT JOIN categorias c ON t.id_categoria = c.id LEFT JOIN cartoes cart ON t.id_cartao = cart.id WHERE t.id_usuario = ? AND t.data_transacao >= ? ORDER BY t.data_transacao ASC", (user_id, inicio_mes_utc_str)); transacoes = cursor.fetchall(); conn.close()
    if not transacoes: await update.effective_message.reply_text("Não há transações neste mês para exportar."); return
    output = io.StringIO(); writer = csv.writer(output, delimiter=';'); writer.writerow(['Data (UTC)', 'Tipo', 'Valor', 'Categoria', 'Forma Pagamento'])
    for data, tipo, valor, cat_nome, cart_nome in transacoes:
        forma_pagamento = cart_nome if cart_nome else 'Dinheiro/Débito'
        writer.writerow([data, tipo, str(valor).replace('.',','), cat_nome.capitalize() if cat_nome else 'Sem Categoria', forma_pagamento])
    output.seek(0); data_bytes = output.getvalue().encode('utf-8'); mes_ano = agora_utc.strftime('%Y_%m'); file_name = f"relatorio_{mes_ano}.csv"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=data_bytes, filename=file_name, caption="Aqui está o seu relatório de transações do mês.")

async def iniciar_processo_transacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.effective_message.text
    padrao = re.compile(r'^([+\-])\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)$'); match = padrao.match(texto)
    if not match: return ConversationHandler.END 
    sinal, valor_str, nome_categoria = match.groups(); nome_categoria = nome_categoria.strip().lower()
    if not nome_categoria: await update.effective_message.reply_text("Adicione uma categoria. Ex: `-50 mercado`"); return ConversationHandler.END
    user_id = get_user_id(update.effective_user.id)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM categorias WHERE id_usuario = ? AND nome = ?", (user_id, nome_categoria)); categoria = cursor.fetchone()
    if not categoria:
        cursor.execute("SELECT nome FROM categorias WHERE id_usuario = ?", (user_id,)); todas_categorias = [cat[0] for cat in cursor.fetchall()]
        conn.close()
        if todas_categorias:
            melhor_sugestao, score = process.extractOne(nome_categoria, todas_categorias, scorer=fuzz.token_sort_ratio)
            if score > 70: 
                context.user_data['sugestao_categoria'] = {'sinal': sinal, 'valor_str': valor_str, 'categoria_errada': nome_categoria, 'sugestao': melhor_sugestao}
                keyboard = [[InlineKeyboardButton(f"Sim, usar '{melhor_sugestao.capitalize()}'", callback_data=f"sugestao_sim"), InlineKeyboardButton("Não, criar nova", callback_data=f"sugestao_nao")]]
                await update.effective_message.reply_text(f"Hmm, não encontrei a categoria '{nome_categoria}'. Quis dizer '{melhor_sugestao.capitalize()}'?", reply_markup=InlineKeyboardMarkup(keyboard))
                return AGUARDANDO_SUGESTAO_CATEGORIA
    conn.close()
    context.user_data['transacao_pendente'] = {'sinal': sinal, 'valor_str': valor_str, 'nome_categoria': nome_categoria}
    if sinal == '+':
        await registrar_transacao_final(update, context, user_id, nome_categoria, sinal, valor_str)
        return ConversationHandler.END
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, nome FROM cartoes WHERE id_usuario = ? ORDER BY nome", (user_id,)); cartoes = cursor.fetchall(); conn.close()
    keyboard = []
    for id_cartao, nome in cartoes: keyboard.append([InlineKeyboardButton(f"💳 {nome}", callback_data=f"cartao:{id_cartao}")])
    keyboard.append([InlineKeyboardButton("💵 Dinheiro/Débito", callback_data="cartao:0")])
    await update.effective_message.reply_text("Como você pagou?", reply_markup=InlineKeyboardMarkup(keyboard))
    return AGUARDANDO_PAGAMENTO

async def tratar_sugestao_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    dados_sugestao = context.user_data.pop('sugestao_categoria', None)
    if not dados_sugestao: await query.edit_message_text("Ocorreu um erro. Tente lançar novamente."); return ConversationHandler.END
    nome_categoria_correta = dados_sugestao['sugestao'] if query.data == 'sugestao_sim' else dados_sugestao['categoria_errada']
    await query.edit_message_text(f"Ok, usando a categoria '{nome_categoria_correta.capitalize()}'...")
    user_id = get_user_id(update.effective_user.id)
    context.user_data['transacao_pendente'] = {'sinal': dados_sugestao['sinal'], 'valor_str': dados_sugestao['valor_str'], 'nome_categoria': nome_categoria_correta}
    if dados_sugestao['sinal'] == '+':
        await registrar_transacao_final(update, context, user_id, nome_categoria_correta, dados_sugestao['sinal'], dados_sugestao['valor_str'])
        return ConversationHandler.END
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, nome FROM cartoes WHERE id_usuario = ? ORDER BY nome", (user_id,)); cartoes = cursor.fetchall(); conn.close()
    keyboard = []
    for id_cartao, nome in cartoes: keyboard.append([InlineKeyboardButton(f"💳 {nome}", callback_data=f"cartao:{id_cartao}")])
    keyboard.append([InlineKeyboardButton("💵 Dinheiro/Débito", callback_data="cartao:0")])
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Como você pagou?", reply_markup=InlineKeyboardMarkup(keyboard))
    return AGUARDANDO_PAGAMENTO

async def receber_forma_pagamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    dados_transacao = context.user_data.pop('transacao_pendente', None)
    if not dados_transacao: await query.edit_message_text("Ocorreu um erro. Tente registar novamente."); return ConversationHandler.END
    user_id = get_user_id(update.effective_user.id)
    id_cartao = int(query.data.split(':')[1]) if query.data.split(':')[1] != '0' else None
    await query.edit_message_text("Ok, registando...")
    await registrar_transacao_final(update, context, user_id, dados_transacao['nome_categoria'], dados_transacao['sinal'], dados_transacao['valor_str'], id_cartao=id_cartao)
    return ConversationHandler.END

async def registrar_transacao_final(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, nome_categoria, sinal, valor_str, id_cartao=None, is_scheduled=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM categorias WHERE id_usuario = ? AND nome = ?", (user_id, nome_categoria))
    categoria = cursor.fetchone()
    
    if not categoria:
        # LÓGICA DE LIMITE DE CATEGORIAS PARA PLANO GRATUITO
        cursor.execute("SELECT data_expiracao FROM assinaturas WHERE id_usuario = ?", (user_id,))
        assinatura = cursor.fetchone()
        is_premium = bool(assinatura and datetime.strptime(assinatura[0], '%Y-%m-%d') >= datetime.now())

        if not is_premium:
            cursor.execute("SELECT COUNT(id) FROM categorias WHERE id_usuario = ?", (user_id,))
            num_categorias = cursor.fetchone()[0]
            if num_categorias >= 3:
                # <<< A MUDANÇA ESTÁ AQUI >>>
                await handle_premium_upsell(update, context, feature_name="3 categorias")
                conn.close()
                return
        
        cursor.execute("INSERT INTO categorias (id_usuario, nome) VALUES (?, ?)", (user_id, nome_categoria)); conn.commit()
        categoria_id = cursor.lastrowid
    else: 
        categoria_id = categoria[0]
        
    tipo = 'saida' if sinal == '-' else 'entrada'
    valor = float(valor_str.replace(',', '.'))
    data_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute("INSERT INTO transacoes (id_usuario, id_categoria, valor, tipo, data_transacao, id_cartao) VALUES (?, ?, ?, ?, ?, ?)", (user_id, categoria_id, valor, tipo, data_str, id_cartao))
    new_transaction_id = cursor.lastrowid
    
    mensagem_sequencia = ""
    if not is_scheduled:
        hoje_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        cursor.execute("SELECT ultimo_lancamento, dias_sequencia FROM usuarios WHERE id = ?", (user_id,))
        ultimo_lancamento, dias_sequencia = cursor.fetchone()
        dias_sequencia = dias_sequencia or 0
        if ultimo_lancamento != hoje_str:
            ontem_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
            nova_sequencia = dias_sequencia + 1 if ultimo_lancamento == ontem_str else 1
            mensagem_sequencia = f"\n\n🔥 Sequência de {nova_sequencia} dias!" if nova_sequencia > 1 else "\n\n💪 Nova sequência iniciada!"
            cursor.execute("UPDATE usuarios SET ultimo_lancamento = ?, dias_sequencia = ? WHERE id = ?", (hoje_str, nova_sequencia, user_id))
    
    mensagem_orcamento = ""
    if tipo == 'saida':
        cursor.execute("SELECT valor FROM orcamentos WHERE id_usuario = ? AND id_categoria = ?", (user_id, categoria_id))
        orcamento = cursor.fetchone()
        if orcamento:
            orcamento_valor = orcamento[0]
            inicio_mes_str = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("SELECT SUM(valor) FROM transacoes WHERE id_usuario = ? AND id_categoria = ? AND tipo = 'saida' AND data_transacao >= ?", (user_id, categoria_id, inicio_mes_str))
            gasto_total_mes = cursor.fetchone()[0] or 0.0
            percentual = (gasto_total_mes / orcamento_valor) * 100
            mensagem_orcamento = f"\n\n💰 *Orçamento:* Você gastou R$ {gasto_total_mes:.2f} de R$ {orcamento_valor:.2f} ({percentual:.1f}%) em '{nome_categoria.capitalize()}' este mês."
            if gasto_total_mes > orcamento_valor:
                mensagem_orcamento += "\n⚠️ *Atenção: Você ultrapassou o orçamento para esta categoria!*"

    conn.commit()
    conn.close()
    
    if is_scheduled:
        await context.bot.send_message(chat_id=context.job.chat_id, text=f"✅ Gasto agendado de '{nome_categoria.capitalize()}' (R$ {valor:.2f}) foi registrado automaticamente.{mensagem_orcamento}", parse_mode='Markdown')
        return
    
     # --- Início da Lógica Corrigida ---

    # 1. Monta a base da mensagem
    respostas_possiveis = ["✅ Anotado!", "Ok, registrado! 👍", "Prontinho!", "Na conta! 📝"]
    mensagem = random.choice(respostas_possiveis)
    detalhes_msg = f"\n**Categoria:** {nome_categoria.capitalize()}\n**Valor:** R$ {valor:.2f}"
    
    # 2. Adiciona detalhes do cartão, se houver
    if id_cartao:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM cartoes WHERE id = ?", (id_cartao,))
        nome_cartao = cursor.fetchone()[0]
        conn.close()
        detalhes_msg += f"\n**Cartão:** {nome_cartao}"
        
    # 3. Compõe a mensagem final
    mensagem_final = mensagem + detalhes_msg + mensagem_sequencia + mensagem_orcamento
    
    # 4. Cria o botão de desfazer
    keyboard = [[InlineKeyboardButton("↩️ Desfazer", callback_data=f"undo:{new_transaction_id}")]]
    
    # 5. Envia a mensagem UMA ÚNICA VEZ
    # Determina o alvo da resposta (se veio de um botão ou de uma mensagem direta)
    target_message = update.callback_query.message if update.callback_query else update.effective_message
    if target_message:
        await target_message.reply_text(
            text=mensagem_final, 
            parse_mode='Markdown', 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def desfazer_lancamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    try: transaction_id = int(query.data.split(':')[1])
    except (IndexError, ValueError): await query.edit_message_text("Erro ao processar."); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM transacoes WHERE id = ?", (transaction_id,))
    if not cursor.fetchone(): await query.edit_message_text("✅ Já foi desfeito.")
    else: cursor.execute("DELETE FROM transacoes WHERE id = ?", (transaction_id,)); conn.commit(); await query.edit_message_text("✅ Lançamento desfeito!")
    conn.close()

async def definir_lembrete_diario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; user_id = get_user_id(update.effective_user.id)
    try:
        horario_str = context.args[0]; fuso_horario = pytz.timezone('America/Sao_Paulo')
        hora, minuto = map(int, horario_str.split(':')); horario_obj = time(hour=hora, minute=minuto, tzinfo=fuso_horario)
    except (IndexError, ValueError): await update.effective_message.reply_text("Uso: `/lembrete HH:MM`"); return
    job_name = f"diario_{chat_id}"
    for job in context.application.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    context.application.job_queue.run_daily(lambda ctx: ctx.bot.send_message(chat_id=ctx.job.chat_id, text=random.choice(["Olá! 👋 Lembre-se de registar seus gastos hoje.", "Ei, como foram as finanças hoje? ✍️"])), time=horario_obj, chat_id=chat_id, name=job_name)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("REPLACE INTO lembretes_diarios (id_usuario, horario, chat_id) VALUES (?, ?, ?)", (user_id, horario_str, chat_id)); conn.commit(); conn.close()
    await update.effective_message.reply_text(f"✅ Lembrete diário configurado para as {horario_str}.")
async def cancelar_lembrete_diario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; user_id = get_user_id(update.effective_user.id); job_name = f"diario_{chat_id}"
    jobs = context.application.job_queue.get_jobs_by_name(job_name)
    if not jobs: await update.effective_message.reply_text("Nenhum lembrete diário ativo."); return
    for job in jobs: job.schedule_removal()
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor(); cursor.execute("DELETE FROM lembretes_diarios WHERE id_usuario = ?", (user_id,)); conn.commit(); conn.close()
    await update.effective_message.reply_text("✅ Lembrete diário cancelado.")

@acesso_premium_necessario
async def agendar_conta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); chat_id = update.effective_chat.id; args = context.args
    try:
        if len(args) < 3: raise ValueError()
        dia = int(args[0]); horario_str = args[1]; valor = None
        try: valor = float(args[2].replace(',', '.')); titulo = " ".join(args[3:])
        except ValueError: titulo = " ".join(args[2:])
        if not (1 <= dia <= 31) or not titulo: raise ValueError()
        hora, minuto = map(int, horario_str.split(':')); fuso_horario = pytz.timezone('America/Sao_Paulo'); horario_obj = time(hour=hora, minute=minuto, tzinfo=fuso_horario)
    except (IndexError, ValueError): await update.effective_message.reply_text("Uso: `/agendar <dia> <HH:MM> [valor] <título>`"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("REPLACE INTO agendamentos (id_usuario, dia, horario, titulo, valor, chat_id) VALUES (?, ?, ?, ?, ?, ?)", (user_id, dia, horario_str, titulo.lower(), valor, chat_id)); id_agendamento = cursor.lastrowid; conn.commit(); conn.close()
    job_name = f"agendamento_{chat_id}_{id_agendamento}"
    for job in context.application.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    callback_func = callback_agendamento if valor is not None else (lambda ctx: ctx.bot.send_message(chat_id=ctx.job.chat_id, text=f"🗓️ Lembrete: Hora de pagar *{ctx.job.data['titulo'].capitalize()}*.", parse_mode='Markdown'))
    context.application.job_queue.run_monthly(callback_func, when=horario_obj, day=dia, name=job_name, chat_id=chat_id, data={'user_id': user_id, 'nome_categoria': titulo.lower(), 'sinal': '-', 'valor_str': str(valor), 'titulo': titulo})
    if valor: await update.effective_message.reply_text(f"✅ Despesa '{titulo.capitalize()}' de R$ {valor:.2f} agendada para todo dia {dia} às {horario_str}!")
    else: await update.effective_message.reply_text(f"✅ Lembrete para '{titulo.capitalize()}' agendado para todo dia {dia} às {horario_str}!")

@acesso_premium_necessario
async def ver_agendamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT dia, horario, titulo, valor FROM agendamentos WHERE id_usuario = ? ORDER BY dia, horario", (user_id,)); agendamentos = cursor.fetchall(); conn.close()
    if not agendamentos: await update.effective_message.reply_text("Nenhuma conta agendada."); return
    resposta = ["🗓️ *Suas Contas Agendadas:*\n"]
    for dia, horario, titulo, valor in agendamentos:
        if valor: resposta.append(f"- *Dia {dia}, {horario}:* {titulo.capitalize()} (R$ {valor:.2f} - Fixo)")
        else: resposta.append(f"- *Dia {dia}, {horario}:* {titulo.capitalize()} (Variável)")
    await update.effective_message.reply_text("\n".join(resposta), parse_mode='Markdown')

@acesso_premium_necessario
async def cancelar_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_id(update.effective_user.id); chat_id = update.effective_chat.id
    try: titulo_para_remover = " ".join(context.args).lower().strip()
    except IndexError: await update.effective_message.reply_text("Uso: `/cancelar_agendamento <título>`"); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id FROM agendamentos WHERE id_usuario = ? AND titulo = ?", (user_id, titulo_para_remover)); agendamento = cursor.fetchone()
    if not agendamento: await update.effective_message.reply_text(f"Não encontrei agendamento com o título '{titulo_para_remover}'."); conn.close(); return
    id_agendamento = agendamento[0]; cursor.execute("DELETE FROM agendamentos WHERE id = ?", (id_agendamento,)); conn.commit(); conn.close()
    job_name = f"agendamento_{chat_id}_{id_agendamento}"
    for job in context.application.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    await update.effective_message.reply_text(f"✅ Agendamento '{titulo_para_remover.capitalize()}' cancelado.")

def carregar_tarefas_agendadas(application: Application):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor(); fuso_horario = pytz.timezone('America/Sao_Paulo')
    cursor.execute("SELECT horario, chat_id FROM lembretes_diarios")
    for horario_str, chat_id in cursor.fetchall():
        hora, minuto = map(int, horario_str.split(':')); horario_obj = time(hour=hora, minute=minuto, tzinfo=fuso_horario)
        application.job_queue.run_daily(lambda ctx: ctx.bot.send_message(chat_id=ctx.job.chat_id, text=random.choice(["Olá! 👋 Lembre-se de registar seus gastos hoje.", "Ei, como foram as finanças hoje? ✍️"])), time=horario_obj, chat_id=chat_id, name=f"diario_{chat_id}")
    print(f"Carregados {cursor.rowcount} lembretes diários.")
    cursor.execute("SELECT id, dia, horario, titulo, valor, chat_id, id_usuario FROM agendamentos")
    agendamentos = cursor.fetchall()
    for id_agendamento, dia, horario_str, titulo, valor, chat_id, user_id in agendamentos:
        hora, minuto = map(int, horario_str.split(':')); horario_obj = time(hour=hora, minute=minuto, tzinfo=fuso_horario)
        job_name = f"agendamento_{chat_id}_{id_agendamento}"
        callback_func = callback_agendamento if valor is not None else (lambda ctx: ctx.bot.send_message(chat_id=ctx.job.chat_id, text=f"🗓️ Lembrete: Hora de pagar *{ctx.job.data['titulo'].capitalize()}*.", parse_mode='Markdown'))
        application.job_queue.run_monthly(callback_func, when=horario_obj, day=dia, name=job_name, chat_id=chat_id, data={'user_id': user_id, 'nome_categoria': titulo, 'sinal': '-', 'valor_str': str(valor), 'titulo': titulo})
    print(f"Carregados {len(agendamentos)} agendamentos de contas.")
    conn.close()

async def apagar_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.effective_message.reply_text("Você não tem permissão para usar este comando.")
        return
    try:
        target_telegram_id = int(context.args[0])
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute("SELECT id FROM usuarios WHERE telegram_id = ?", (target_telegram_id,)); user = cursor.fetchone()
        if not user:
            await update.effective_message.reply_text(f"Usuário com ID do Telegram {target_telegram_id} não encontrado."); conn.close()
            return
        id_interno = user[0]
        cursor.execute("DELETE FROM transacoes WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM orcamentos WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM cartoes WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM categorias WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM lembretes_diarios WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM agendamentos WHERE id_usuario = ?", (id_interno,))
        cursor.execute("DELETE FROM usuarios WHERE id = ?", (id_interno,)); conn.commit(); conn.close()
        await update.effective_message.reply_text(f"Todos os dados do usuário com ID {target_telegram_id} foram apagados com sucesso.")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Uso: /apagarusuario <ID do Telegram do usuário>")

async def enviar_insight_semanal(context: ContextTypes.DEFAULT_TYPE):
    """Calcula e envia o insight da semana para um usuário específico."""
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # ### VERIFICAÇÃO PREMIUM ###
    # Verifica se o usuário ainda tem uma assinatura ativa antes de enviar o insight
    cursor.execute("SELECT data_expiracao FROM assinaturas WHERE id_usuario = ?", (user_id,))
    assinatura = cursor.fetchone()
    if not (assinatura and datetime.strptime(assinatura[0], '%Y-%m-%d') >= datetime.now()):
        conn.close()
        logger.info(f"Usuário {user_id} não é mais premium. Insight semanal não enviado.")
        return # Para a execução se o usuário não for premium
    # ### FIM DA VERIFICAÇÃO ###
    
    # Calcula a data de 7 dias atrás
    sete_dias_atras = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    
    # Query para encontrar a categoria com maior gasto na última semana
    cursor.execute("""
        SELECT c.nome, SUM(t.valor) as total_gasto
        FROM transacoes t
        JOIN categorias c ON t.id_categoria = c.id
        WHERE t.id_usuario = ? AND t.tipo = 'saida' AND t.data_transacao >= ?
        GROUP BY c.nome
        ORDER BY total_gasto DESC
        LIMIT 1
    """, (user_id, sete_dias_atras))
    
    maior_gasto = cursor.fetchone()
    conn.close()
    
    if maior_gasto:
        nome_categoria, total_gasto = maior_gasto
        mensagem = (
            f"💡 *Seu Insight da Semana Premium!*\n\n"
            f"Nos últimos 7 dias, sua maior categoria de gastos foi *{nome_categoria.capitalize()}*, "
            f"totalizando *R$ {total_gasto:.2f}*.\n\n"
            f"Continue registrando para mais insights! 😉"
        )
        await context.bot.send_message(chat_id=chat_id, text=mensagem, parse_mode='Markdown')
        
def agendar_insights_semanais(application: Application):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id FROM usuarios WHERE chat_id IS NOT NULL"); usuarios = cursor.fetchall(); conn.close()
    fuso_horario = pytz.timezone('America/Sao_Paulo'); horario_envio = time(10, 0, tzinfo=fuso_horario)
    for user_id, chat_id in usuarios:
        job_name = f"insight_semanal_{user_id}"
        for job in application.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        application.job_queue.run_daily(enviar_insight_semanal, time=horario_envio, days=(0,), chat_id=chat_id, name=job_name, data={"user_id": user_id, "chat_id": chat_id})
    print(f"Agendados insights semanais para {len(usuarios)} usuários.")

async def callback_agendamento(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data.get('user_id'); nome_categoria = job_data.get('nome_categoria'); sinal = job_data.get('sinal'); valor_str = job_data.get('valor_str')
    await registrar_transacao_final(update=None, context=context, user_id=user_id, nome_categoria=nome_categoria, sinal=sinal, valor_str=valor_str, is_scheduled=True)

async def handle_premium_upsell(update: Update, context: ContextTypes.DEFAULT_TYPE, feature_name: str):
    """
    Envia uma mensagem padronizada e interativa quando um usuário gratuito atinge um limite.
    """
    texto = (
        f"💎 **Você atingiu o limite de {feature_name} do plano gratuito!**\n\n"
        "Vejo que você está organizando bem suas finanças! Para levar seu controle a um novo patamar, o plano **Premium** oferece:\n\n"
        "✅ **Categorias e Cartões ilimitados**\n"
        "📊 **Relatórios com gráficos detalhados**\n"
        "💰 **Criação de orçamentos por categoria**\n"
        "⏰ **Agendamento de contas e lembretes**\n\n"
        "Libere todo o potencial do bot e tenha uma visão completa da sua vida financeira!"
    )
    
    # Você precisará criar um handler para 'upgrade_premium' que envie os detalhes da assinatura
    keyboard = [
        [InlineKeyboardButton("✨ Fazer Upgrade Agora", callback_data="upgrade_premium")],
        [InlineKeyboardButton("✖️ Agora não", callback_data="dismiss_upsell")]
    ]

    # Se o limite for de categorias, ofereça a opção de gerenciá-las
    if "categoria" in feature_name:
        keyboard.insert(1, [InlineKeyboardButton("🗂️ Gerenciar minhas categorias", callback_data="manage_categories")])
        
    await update.effective_message.reply_text(
        text=texto,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Crie também os handlers para os botões que você adicionou
async def dismiss_upsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Entendido. Continue aproveitando os recursos gratuitos! 😉")

# O handler de 'manage_categories' pode simplesmente chamar sua função list_categorias
async def manage_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Aqui estão suas categorias atuais. Você pode usar /del_categoria para remover alguma.")
    await list_categorias(update, context) # Reutiliza sua função existente

def main():
    inicializar_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("ERRO: A variável de ambiente TELEGRAM_TOKEN não foi definida.")
        return
    application = Application.builder().token(TOKEN).build()
    
    carregar_tarefas_agendadas(application)
    agendar_insights_semanais(application)

    onboarding_conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        ONBOARDING_INICIO: [
            CallbackQueryHandler(onboarding_iniciar, pattern='^onboarding_start$'),
            CallbackQueryHandler(onboarding_finalizar, pattern='^onboarding_skip_all$')
        ],
        ONBOARDING_ORCAMENTO: [
            CommandHandler('add_cartao', add_cartao),
            CallbackQueryHandler(onboarding_pular_cartao, pattern='^onboarding_skip_card$')
        ],
        # ESTE ESTADO FOI ATUALIZADO
        ONBOARDING_TRANSACAO: [
            # Espera por uma mensagem de texto que pareça uma transação
            MessageHandler(filters.Regex(r'^[+\-]\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)'), finalizar_onboarding_com_transacao),
            # Ainda permite finalizar o tour com o botão
            CallbackQueryHandler(onboarding_finalizar, pattern='^onboarding_skip_all$')
        ],
    },
    fallbacks=[CommandHandler('start', start)],
)
    
    transacao_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^[+\-]\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)'), iniciar_processo_transacao)],
        states={
            AGUARDANDO_PAGAMENTO: [CallbackQueryHandler(receber_forma_pagamento, pattern="^cartao:")],
            AGUARDANDO_SUGESTAO_CATEGORIA: [CallbackQueryHandler(tratar_sugestao_categoria, pattern="^sugestao_")],
        },
        fallbacks=[CommandHandler('cancelar', cancelar_conversa)],
    )

    relatorio_conv = ConversationHandler(
        entry_points=[
            CommandHandler('relatorio', iniciar_relatorio),
            MessageHandler(filters.Regex('^📊 Relatório$'), iniciar_relatorio)
        ],
        states={
            ESCOLHER_PERIODO: [CallbackQueryHandler(processar_escolha_periodo, pattern="^rel_")],
            AGUARDANDO_DATA_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_data_inicio)],
            AGUARDANDO_DATA_FIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_data_fim)],
        },
        fallbacks=[CommandHandler('cancelar', cancelar_conversa)],
    )

    application.add_handler(onboarding_conv)
    application.add_handler(transacao_conv)
    application.add_handler(relatorio_conv)


# ... no seu main()
    application.add_handler(CallbackQueryHandler(dismiss_upsell, pattern="^dismiss_upsell$"))
    application.add_handler(CallbackQueryHandler(manage_categories_callback, pattern="^manage_categories$"))
# Adicione também o handler para 'upgrade_premium'
    #application.add_handler(CallbackQueryHandler(funcao_de_assinar, pattern="^upgrade_premium$"))
# Commandos que não fazem parte de conversas
    application.add_handler(CommandHandler("ajuda", ajuda))
    application.add_handler(CommandHandler("add_cartao", add_cartao))
    application.add_handler(CommandHandler("listarcategorias", list_categorias))
    application.add_handler(CommandHandler("del_categoria", del_categoria))
    application.add_handler(CommandHandler("exportar", exportar_csv))
    application.add_handler(CommandHandler("list_cartoes", list_cartoes))
    application.add_handler(CommandHandler("fatura", fatura))
    application.add_handler(CommandHandler("del_cartao", del_cartao))
    application.add_handler(CommandHandler("lembrete", definir_lembrete_diario))
    application.add_handler(CommandHandler("cancelar_lembrete", cancelar_lembrete_diario))
    application.add_handler(CommandHandler("agendar", agendar_conta))
    application.add_handler(CommandHandler("ver_agendamentos", ver_agendamentos))
    application.add_handler(CommandHandler("cancelar_agendamento", cancelar_agendamento))
    application.add_handler(CommandHandler("meus_orcamentos", list_orcamentos))
    application.add_handler(CommandHandler("del_orcamento", del_orcamento))
    application.add_handler(CommandHandler("apagarusuario", apagar_usuario))
    # Botões do menu que não são entry points
    application.add_handler(MessageHandler(filters.Regex('^🗂️ Categorias$'), list_categorias))
    application.add_handler(MessageHandler(filters.Regex('^💳 Cartões$'), menu_cartoes))
    
    application.add_handler(MessageHandler(filters.Regex('^💡 Ajuda$'), ajuda))
    application.add_handler(MessageHandler(filters.Regex('^⏰ Lembretes/Agendamentos$'), menu_lembretes_e_agendamentos))
    application.add_handler(MessageHandler(filters.Regex('^⬇️ Exportar$'), exportar_csv))
    application.add_handler(MessageHandler(filters.Regex('^🏠 Menu Principal$'), start))

    logger.info("Bot v23 (Paywall Completo) iniciado!")
    application.run_polling()

if __name__ == '__main__':
    main()