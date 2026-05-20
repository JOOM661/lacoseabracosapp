import flet as ft
import os
import asyncio
import requests
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv
from payment_server import start_payment_server, get_payment_state, reset_payment_state

load_dotenv()

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SRV_KEY  = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_FROM = os.getenv("GMAIL_FROM", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")

ADMIN_IPS = set(
    ip.strip() for ip in os.getenv("ADMIN_IPS", "127.0.0.1,::1,191.0.100.111").split(",") if ip.strip()
)

LOCAL_PORT = 8080
URL_SERVER = "https://lacoseabracos.onrender.com"  # <--- APONTANDO PARA A NUVEM

# ── Paleta ────────────────────────────────────────────────────────────────────
BG       = "#FFF0F5"
CARD_BG  = "#FFFFFF"
PRIMARY  = "#D81B60"
PRIMARY2 = "#F06292"
DARK     = "#3D1A28"
MUTED    = "#9E7080"
SUCCESS  = "#2E7D32"
SUCCESS2 = "#66BB6A"
ERROR_C  = "#C62828"
ACCENT   = "#FF80AB"
BLUE     = "#009EE3"


# ── Supabase REST ─────────────────────────────────────────────────────────────
def _hdrs(admin=False, returning=True):
    k = SUPABASE_SRV_KEY if admin else SUPABASE_ANON_KEY
    h = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    if returning:
        h["Prefer"] = "return=representation"
    return h

def sb_get(table, params="", admin=False):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=_hdrs(admin), timeout=10)
        r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"[sb_get] {e}"); return []

def sb_post(table, data, admin=False):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=_hdrs(admin, True), json=data, timeout=10)
        r.raise_for_status(); return r.json() if r.text else {"ok": True}
    except Exception as e:
        print(f"[sb_post] ERRO: {e}"); return None

def sb_patch(table, match, data, admin=False):
    try:
        r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{match}", headers=_hdrs(admin), json=data, timeout=10)
        r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"[sb_patch] {e}"); return None

def sb_delete(table, match, admin=False):
    try:
        r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?{match}", headers=_hdrs(admin), timeout=10)
        r.raise_for_status(); return True
    except Exception as e:
        print(f"[sb_delete] {e}"); return False

# ── Auth com tabela própria + bcrypt ─────────────────────────────────────────
# Senhas NUNCA são salvas em texto puro — sempre hash bcrypt (custo 12)
import bcrypt as _bcrypt

def _hash_senha(senha: str) -> str:
    """Gera hash bcrypt da senha."""
    return _bcrypt.hashpw(senha.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")

def _check_senha(senha: str, hashed: str) -> bool:
    """Verifica senha contra hash armazenado."""
    try:
        return _bcrypt.checkpw(senha.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def sb_signup(email, password, nome):
    """Cadastra usuário: salva hash bcrypt na tabela usuarios + Supabase Auth."""
    # 1. Verifica se email já existe na tabela própria
    existente = sb_get("usuarios", f"email=eq.{email}&select=id", admin=True)
    if existente:
        return {"ok": False, "msg": "E-mail já cadastrado. Faça login."}

    # 2. Valida senha mínima
    if len(password) < 6:
        return {"ok": False, "msg": "Senha deve ter no mínimo 6 caracteres."}

    # 3. Gera hash bcrypt
    senha_hash = _hash_senha(password)

    # 4. Salva na tabela usuarios (service_role para bypassar RLS)
    usuario_data = {
        "email":      email,
        "nome":       nome,
        "senha_hash": senha_hash,
        "is_admin":   False,
        "criado_em":  datetime.now(timezone.utc).isoformat(),
    }
    res = sb_post("usuarios", usuario_data, admin=True)
    if not res:
        return {"ok": False, "msg": "Erro ao criar conta. Tente novamente."}

    usuario = res[0] if isinstance(res, list) else res
    return {
        "ok": True,
        "user": {
            "id":    usuario.get("id", ""),
            "email": email,
            "user_metadata": {"nome": nome},
            "is_admin": False,
        },
        "session": None,
    }

def sb_signin(email, password):
    """Login: busca usuário, verifica hash bcrypt."""
    if not email or not password:
        return {"ok": False, "msg": "Preencha e-mail e senha."}

    # Busca usuário pelo email (service_role para bypassar RLS)
    rows = sb_get("usuarios", f"email=eq.{email}&select=*", admin=True)
    if not rows:
        return {"ok": False, "msg": "E-mail não encontrado."}

    usuario = rows[0]
    senha_hash = usuario.get("senha_hash", "")

    if not senha_hash:
        return {"ok": False, "msg": "Conta sem senha configurada. Contate o suporte."}

    if not _check_senha(password, senha_hash):
        return {"ok": False, "msg": "Senha incorreta."}

    return {
        "ok": True,
        "user": {
            "id":            usuario.get("id", ""),
            "email":         email,
            "user_metadata": {"nome": usuario.get("nome", email.split("@")[0])},
            "is_admin":      usuario.get("is_admin", False),
        },
        "session": {"access_token": "local"},
    }


# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass


# ── Email via Gmail SMTP ─────────────────────────────────────────────────────
def send_email(to: str, cesta_nome: str, cesta_preco: float, destinatario: str,
               data_entrega: str, periodo: str, msg_cartao: str, nome_cliente: str):
    """Envia email de confirmação de pedido via Gmail SMTP."""
    if not GMAIL_FROM or not GMAIL_PASS:
        print("[email] GMAIL_FROM ou GMAIL_APP_PASSWORD não configurados")
        return False
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#FFF0F5;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#FFF0F5;padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:24px;overflow:hidden;box-shadow:0 8px 32px rgba(216,27,96,0.12);">

        <tr>
          <td style="background:linear-gradient(135deg,#D81B60,#F06292);padding:36px 40px;text-align:center;">
            <div style="font-size:48px;margin-bottom:8px;">🌸</div>
            <h1 style="color:#ffffff;margin:0;font-size:26px;font-weight:900;letter-spacing:-0.5px;">Laços & Abraços</h1>
            <p style="color:#FFD6E7;margin:6px 0 0;font-size:14px;">Feito com carinho, entregue com amor</p>
          </td>
        </tr>

        <tr>
          <td style="padding:36px 40px 0;">
            <div style="text-align:center;margin-bottom:28px;">
              <div style="display:inline-block;background:linear-gradient(135deg,#2E7D32,#66BB6A);
                          border-radius:50%;width:72px;height:72px;line-height:72px;
                          font-size:36px;margin-bottom:16px;">✅</div>
              <h2 style="color:#3D1A28;font-size:22px;font-weight:900;margin:0 0 8px;">
                Pagamento Confirmado! 🎉
              </h2>
              <p style="color:#9E7080;font-size:14px;margin:0;">
                Olá, <strong>{nome_cliente}</strong>! Seu pedido foi recebido com sucesso.
              </p>
            </div>

            <div style="background:#FFF5F9;border-radius:16px;padding:24px;margin-bottom:20px;
                        border-left:4px solid #D81B60;">
              <h3 style="color:#D81B60;font-size:15px;font-weight:800;margin:0 0 16px;">
                📦 Detalhes do Pedido
              </h3>
              <table width="100%" cellpadding="6" cellspacing="0">
                <tr>
                  <td style="color:#9E7080;font-size:13px;width:40%;">🛍️ Cesta</td>
                  <td style="color:#3D1A28;font-size:13px;font-weight:700;">{cesta_nome}</td>
                </tr>
                <tr>
                  <td style="color:#9E7080;font-size:13px;">💰 Valor pago</td>
                  <td style="color:#2E7D32;font-size:15px;font-weight:900;">R$ {cesta_preco:.2f}</td>
                </tr>
                <tr>
                  <td style="color:#9E7080;font-size:13px;">🎁 Para</td>
                  <td style="color:#3D1A28;font-size:13px;font-weight:700;">{destinatario if destinatario else "—"}</td>
                </tr>
                <tr>
                  <td style="color:#9E7080;font-size:13px;">📅 Entrega</td>
                  <td style="color:#3D1A28;font-size:13px;">{data_entrega if data_entrega else "A combinar"}{f" ({periodo})" if periodo else ""}</td>
                </tr>
                {f'<tr><td style="color:#9E7080;font-size:13px;">💌 Cartão</td><td style="color:#3D1A28;font-size:13px;font-style:italic;">"{msg_cartao}"</td></tr>' if msg_cartao else ""}
              </table>
            </div>

            <div style="background:#F0FFF4;border-radius:16px;padding:20px;margin-bottom:24px;
                        border-left:4px solid #2E7D32;">
              <h3 style="color:#2E7D32;font-size:14px;font-weight:800;margin:0 0 12px;">
                ✅ Próximos passos
              </h3>
              <p style="color:#555;font-size:13px;margin:0 0 8px;">
                📱 Entraremos em contato pelo <strong>WhatsApp</strong> para confirmar os detalhes da entrega.
              </p>
              <p style="color:#555;font-size:13px;margin:0;">
                🌸 Sua cesta será preparada com todo o carinho especial que ela merece!
              </p>
            </div>

            <div style="text-align:center;padding:20px;border-radius:16px;background:#FFF5F9;margin-bottom:28px;">
              <p style="color:#9E7080;font-size:13px;font-style:italic;margin:0;line-height:1.6;">
                "Cada cesta é preparada com amor e dedicação.<br>
                Sua escolha vai fazer alguém muito especial sorrir hoje." 🌷
              </p>
            </div>
          </td>
        </tr>

        <tr>
          <td style="background:#FFF0F5;padding:24px 40px;text-align:center;border-top:1px solid #F0D0DC;">
            <p style="color:#9E7080;font-size:12px;margin:0;">
              Laços & Abraços • Feito com 💝
            </p>
            <p style="color:#C0A0B0;font-size:11px;margin:4px 0 0;">
              Este é um e-mail automático, não responda.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Pedido Confirmado — Laços & Abraços 🌸"
        msg["From"]    = f"Laços & Abraços <{GMAIL_FROM}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_FROM, GMAIL_PASS)
            smtp.sendmail(GMAIL_FROM, to, msg.as_string())

        print(f"[email] Enviado para {to}")
        return True
    except Exception as e:
        print(f"[email] ERRO: {e}")
        return False


# ── Admin por IP ──────────────────────────────────────────────────────────────
def check_is_admin(page):
    ip = getattr(page, "client_ip", None) or ""
    return ip in ADMIN_IPS or ip in {"", "127.0.0.1", "::1", "localhost"}


# ── UI helpers ────────────────────────────────────────────────────────────────
def btn(label, on_click, color=None, width=200, height=46, icon=None):
    row_controls = []
    if icon:
        row_controls.append(ft.Icon(icon, color="#FFFFFF", size=18))
    row_controls.append(ft.Text(label, color="#FFFFFF", weight=ft.FontWeight.W_700, size=15))
    return ft.Container(
        width=width, height=height,
        gradient=ft.LinearGradient(
            begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1),
            colors=[color or PRIMARY, PRIMARY2 if (color is None or color == PRIMARY) else color],
        ),
        border_radius=24,
        alignment=ft.Alignment(0, 0),
        on_click=on_click,
        shadow=ft.BoxShadow(blur_radius=12, spread_radius=0, color="#50D81B60", offset=ft.Offset(0, 4)),
        content=ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8, controls=row_controls),
    )

def snack(page, msg, ok=True):
    sb = ft.SnackBar(
        content=ft.Row(spacing=10, controls=[
            ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR, color="#FFFFFF", size=20),
            ft.Text(msg, color="#FFFFFF", weight=ft.FontWeight.W_600, size=14),
        ]),
        bgcolor=SUCCESS if ok else ERROR_C,
        duration=3500,
        behavior=ft.SnackBarBehavior.FLOATING,
        shape=ft.RoundedRectangleBorder(radius=14),
        margin=ft.Margin(16, 0, 16, 16),
    )
    page.overlay.append(sb)
    sb.open = True
    page.update()

def mk_field(label, password=False, multiline=False, max_lines=1, width=320, kb=None, hint=None, icon=None):
    kw = dict(
        label=label, width=width,
        border_color=PRIMARY, border_radius=14,
        focused_border_color=PRIMARY, focused_border_width=2,
        password=password, multiline=multiline, max_lines=max_lines,
        text_size=14, label_style=ft.TextStyle(color=MUTED, size=13),
        cursor_color=PRIMARY,
    )
    if kb:      kw["keyboard_type"] = kb
    if hint:    kw["hint_text"] = hint
    if icon:    kw["prefix_icon"] = icon
    if password: kw["can_reveal_password"] = True
    return ft.TextField(**kw)

def section_title(text, emoji=""):
    return ft.Row(spacing=8, controls=[
        ft.Text(emoji, size=20) if emoji else ft.Container(),
        ft.Text(text, size=16, weight=ft.FontWeight.W_700, color=DARK),
    ])

def divider():
    return ft.Container(height=1, bgcolor="#F0D0DC", margin=ft.Margin(0, 8, 0, 8))


# ── AUTH VIEW ────────────────────────────────────────────────────────────────
def auth_view(page, on_success):
    """Tela de login e cadastro."""
    mode        = ["login"]   # "login" | "signup"
    loading     = [False]

    email_f   = mk_field("E-mail", kb=ft.KeyboardType.EMAIL, icon=ft.Icons.EMAIL_OUTLINED, width=340)
    senha_f   = mk_field("Senha", password=True, icon=ft.Icons.LOCK_OUTLINE, width=340)
    nome_f    = mk_field("Seu nome completo", icon=ft.Icons.PERSON_OUTLINE, width=340)
    spin      = ft.ProgressRing(color=PRIMARY, width=28, height=28, visible=False, stroke_width=3)
    erro_t    = ft.Text("", color=ERROR_C, size=13, text_align=ft.TextAlign.CENTER)

    nome_container = ft.AnimatedSwitcher(
        content=ft.Container(height=0),
        transition=ft.AnimatedSwitcherTransition.FADE,
        duration=200,
    )

    title_t   = ft.Text("Entrar na sua conta", size=22, weight=ft.FontWeight.W_800, color=DARK)
    sub_t     = ft.Text("Bem-vinda de volta! 🌸", size=14, color=MUTED)
    toggle_t  = ft.Text("Não tem conta? Cadastre-se", size=13, color=PRIMARY, weight=ft.FontWeight.W_600)
    action_lbl= ft.Text("Entrar", color="#FFFFFF", weight=ft.FontWeight.W_700, size=16)

    action_btn = ft.Container(
        width=340, height=50,
        gradient=ft.LinearGradient(begin=ft.Alignment(-1, 0), end=ft.Alignment(1, 0), colors=[PRIMARY, PRIMARY2]),
        border_radius=25,
        alignment=ft.Alignment(0, 0),
        shadow=ft.BoxShadow(blur_radius=16, color="#60D81B60", offset=ft.Offset(0, 5)),
        content=ft.Stack(
            controls=[
                ft.Container(alignment=ft.Alignment(0, 0), content=action_lbl),
                ft.Container(alignment=ft.Alignment(0, 0), content=spin),
            ],
        ),
    )

    def set_loading(v):
        loading[0] = v
        action_lbl.visible = not v
        spin.visible = v
        page.update()

    def toggle_mode(e):
        if mode[0] == "login":
            mode[0] = "signup"
            title_t.value  = "Criar sua conta"
            sub_t.value    = "É rapidinho! 🌷"
            action_lbl.value = "Criar Conta"
            toggle_t.value = "Já tem conta? Entrar"
            nome_container.content = nome_f
        else:
            mode[0] = "login"
            title_t.value  = "Entrar na sua conta"
            sub_t.value    = "Bem-vinda de volta! 🌸"
            action_lbl.value = "Entrar"
            toggle_t.value = "Não tem conta? Cadastre-se"
            nome_container.content = ft.Container(height=0)
        erro_t.value = ""
        page.update()

    async def submit(e):
        if loading[0]:
            return
        em = email_f.value.strip()
        pw = senha_f.value.strip()
        if not em or not pw:
            erro_t.value = "Preencha e-mail e senha."; page.update(); return
        set_loading(True)
        erro_t.value = ""

        if mode[0] == "signup":
            nm = nome_f.value.strip()
            if not nm:
                erro_t.value = "Preencha seu nome."; set_loading(False); return
            res = await asyncio.to_thread(sb_signup, em, pw, nm)
        else:
            res = await asyncio.to_thread(sb_signin, em, pw)

        set_loading(False)
        if res["ok"]:
            user_data = {
                "email":    res["user"].get("email", em),
                "nome":     res["user"].get("user_metadata", {}).get("nome", em.split("@")[0]),
                "id":       res["user"].get("id", ""),
                "is_admin": res["user"].get("is_admin", False),
            }
            on_success(user_data)
        else:
            erro_t.value = res["msg"]
            page.update()

    action_btn.on_click = lambda e: page.run_task(submit, e)
    toggle_t_container  = ft.Container(on_click=toggle_mode, content=toggle_t, padding=ft.Padding(4,4,4,4))

    return ft.View(
        route="/auth",
        bgcolor=BG,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Container(
                expand=True,
                padding=ft.Padding(24, 60, 24, 40),
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=0,
                    controls=[
                        # Logo
                        ft.Container(
                            width=90, height=90,
                            gradient=ft.LinearGradient(
                                begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1),
                                colors=[PRIMARY, ACCENT],
                            ),
                            border_radius=45,
                            alignment=ft.Alignment(0, 0),
                            shadow=ft.BoxShadow(blur_radius=24, color="#50D81B60", offset=ft.Offset(0, 8)),
                            content=ft.Text("🌸", size=44),
                        ),
                        ft.Container(height=28),
                        title_t,
                        ft.Container(height=6),
                        sub_t,
                        ft.Container(height=32),
                        # Card do form
                        ft.Container(
                            width=380,
                            bgcolor=CARD_BG,
                            border_radius=24,
                            padding=ft.Padding(28, 28, 28, 28),
                            shadow=ft.BoxShadow(blur_radius=30, spread_radius=-4, color="#20D81B60", offset=ft.Offset(0, 10)),
                            content=ft.Column(
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=14,
                                controls=[
                                    nome_container,
                                    email_f,
                                    senha_f,
                                    ft.Container(height=4),
                                    erro_t,
                                    action_btn,
                                    ft.Container(height=4),
                                    divider(),
                                    toggle_t_container,
                                ],
                            ),
                        ),
                        ft.Container(height=20),
                        ft.Text(
                            "Seus dados estão seguros conosco 🔒",
                            size=12, color=MUTED,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    ],
                ),
            ),
        ],
    )


# ── HOME VIEW ─────────────────────────────────────────────────────────────────
def home_view(page, go_pedido, go_admin, is_admin, usuario):
    spin      = ft.ProgressRing(color=PRIMARY, width=40, height=40, stroke_width=3)
    cards_col = ft.Column(spacing=20)
    note      = ft.Text("", color=MUTED, size=13)

    nome_usuario = usuario.get("nome", "").split()[0] if usuario else ""

    async def load():
        cestas = await asyncio.to_thread(sb_get, "cestas", "order=created_at.asc&ativo=eq.true")
        spin.visible = False
        if not cestas:
            note.value = "Nenhuma cesta disponível no momento 🌷"
            page.update(); return

        for c in cestas:
            cor     = c.get("cor", PRIMARY)
            emoji   = c.get("emoji", "🧺")
            nome    = c.get("nome", "Cesta Especial")
            preco   = float(c.get("preco", 0))
            itens   = str(c.get("itens", ""))
            img_url = c.get("imagem_url", "")
            cid     = c.get("id")

            if img_url:
                imagem_visual = ft.Image(src=img_url, fit=ft.BoxFit.COVER, width=120, height=150)
            else:
                imagem_visual = ft.Container(
                    bgcolor=cor, width=120, height=150,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(emoji, size=52),
                )

            cards_col.controls.append(
                ft.Container(
                    bgcolor=CARD_BG,
                    border_radius=20,
                    shadow=ft.BoxShadow(blur_radius=20, spread_radius=-2, color="#28D81B60", offset=ft.Offset(0, 6)),
                    content=ft.Row(
                        spacing=0,
                        controls=[
                            ft.Container(
                                border_radius=ft.BorderRadius(20, 0, 0, 20),
                                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                                content=imagem_visual,
                            ),
                            ft.Container(
                                expand=True,
                                padding=ft.Padding(16, 14, 16, 14),
                                content=ft.Column(
                                    spacing=6,
                                    controls=[
                                        ft.Text(nome, size=17, weight=ft.FontWeight.W_800, color=DARK),
                                        ft.Text(
                                            itens[:70] + ("…" if len(itens) > 70 else ""),
                                            size=12, color=MUTED,
                                        ),
                                        ft.Container(height=6),
                                        ft.Row(
                                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                            controls=[
                                                ft.Column(spacing=0, controls=[
                                                    ft.Text("a partir de", size=10, color=MUTED),
                                                    ft.Text(f"R$ {preco:.2f}", size=20, weight=ft.FontWeight.W_900, color=PRIMARY),
                                                ]),
                                                ft.Container(
                                                    gradient=ft.LinearGradient(
                                                        begin=ft.Alignment(-1, 0), end=ft.Alignment(1, 0),
                                                        colors=[PRIMARY, PRIMARY2],
                                                    ),
                                                    border_radius=20,
                                                    padding=ft.Padding(14, 8, 14, 8),
                                                    shadow=ft.BoxShadow(blur_radius=8, color="#40D81B60", offset=ft.Offset(0, 3)),
                                                    on_click=lambda e, i=cid, n=nome, p=preco: go_pedido(i, n, p),
                                                    content=ft.Text("Comprar 💝", color="#FFFFFF", weight=ft.FontWeight.W_700, size=13),
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ),
                        ],
                    ),
                )
            )
        page.update()

    page.run_task(load)

    saudacao = ft.Row(spacing=8, controls=[
        ft.Text(f"Olá, {nome_usuario}! 🌸" if nome_usuario else "Bem-vinda! 🌸",
                size=15, color=MUTED, weight=ft.FontWeight.W_500),
    ]) if usuario else ft.Container(height=0)

    admin_btn = ft.Container(
        visible=is_admin,
        padding=ft.Padding(12, 6, 16, 6),
        border_radius=10,
        bgcolor="#FFE4EE",
        on_click=lambda e: go_admin(),
        content=ft.Row(spacing=6, controls=[
            ft.Icon(ft.Icons.SETTINGS, color=PRIMARY, size=16),
            ft.Text("Painel", color=PRIMARY, size=13, weight=ft.FontWeight.W_700),
        ]),
    )

    return ft.View(
        route="/",
        bgcolor=BG,
        scroll=ft.ScrollMode.AUTO,
        appbar=ft.AppBar(
            title=ft.Row(spacing=6, controls=[
                ft.Text("🌸", size=22),
                ft.Text("Laços & Abraços", color=PRIMARY, weight=ft.FontWeight.W_800, size=20),
            ]),
            center_title=False,
            bgcolor=CARD_BG,
            elevation=2,
            shadow_color="#10D81B60",
            actions=[admin_btn, ft.Container(width=8)],
        ),
        controls=[
            ft.Container(
                padding=ft.Padding(20, 20, 20, 40),
                content=ft.Column(controls=[
                    saudacao,
                    ft.Container(height=4),
                    ft.Text("Presenteie quem você ama 💝", size=24, weight=ft.FontWeight.W_900, color=DARK),
                    ft.Text("Cestas artesanais, entregues com carinho.", size=14, color=MUTED),
                    ft.Container(height=24),
                    spin, note, cards_col,
                ]),
            ),
        ],
    )


# ── PEDIDO VIEW ───────────────────────────────────────────────────────────────
def pedido_view(page, cesta_id, cesta_nome, cesta_preco, go_home, go_confirmacao, usuario):
    # Campos
    nome_f       = mk_field("Nome completo", icon=ft.Icons.PERSON_OUTLINE, width=340,
                             hint=usuario.get("nome", "") if usuario else "")
    tel_f        = mk_field("WhatsApp com DDD", icon=ft.Icons.PHONE_OUTLINED,
                             kb=ft.KeyboardType.PHONE, width=340, hint="(11) 99999-9999")
    end_f        = mk_field("Endereço completo de entrega", icon=ft.Icons.LOCATION_ON_OUTLINED,
                             multiline=True, max_lines=3, width=340, hint="Rua, número, bairro, cidade")
    cep_f        = mk_field("CEP", icon=ft.Icons.MAP_OUTLINED, kb=ft.KeyboardType.NUMBER, width=160)
    data_f       = mk_field("Data desejada para entrega", icon=ft.Icons.CALENDAR_TODAY_OUTLINED,
                             width=340, hint="Ex: 25/06/2025")
    periodo_f    = mk_field("Período preferido", icon=ft.Icons.ACCESS_TIME_OUTLINED,
                             width=340, hint="Manhã, tarde ou noite?")
    destinatario_f = mk_field("Nome de quem vai receber", icon=ft.Icons.CARD_GIFTCARD_OUTLINED, width=340)
    msg_cartao_f = mk_field("Mensagem para o cartão 💌", icon=ft.Icons.FAVORITE_BORDER,
                             multiline=True, max_lines=3, width=340,
                             hint="Escreva algo especial...")
    obs_f        = mk_field("Observações adicionais", icon=ft.Icons.NOTES_OUTLINED,
                             multiline=True, max_lines=2, width=340)

    # Como soube — dropdown visual
    como_soube_val = [""]
    como_opcoes    = ["Instagram", "Indicação de amigo(a)", "WhatsApp", "Google", "TikTok", "Outro"]
    como_chips     = ft.Row(wrap=True, spacing=8, run_spacing=8, controls=[])

    def make_chip(opcao):
        selected = [False]
        chip_ref = [None]
        def on_tap(e, op=opcao, s=selected, cr=chip_ref):
            s[0] = not s[0]
            como_soube_val[0] = op if s[0] else ""
            # Reset outros chips
            for c in como_chips.controls:
                if c is not cr[0]:
                    c.bgcolor = "#F8EEF2"
                    c.border  = ft.Border.all(1, "#E8D0DA")
                    c.content.controls[0].color = MUTED
            cr[0].bgcolor = PRIMARY if s[0] else "#F8EEF2"
            cr[0].border  = ft.Border.all(1.5, PRIMARY if s[0] else "#E8D0DA")
            cr[0].content.controls[0].color = "#FFFFFF" if s[0] else MUTED
            page.update()
        chip = ft.Container(
            padding=ft.Padding(14, 8, 14, 8),
            border_radius=20,
            bgcolor="#F8EEF2",
            border=ft.Border.all(1, "#E8D0DA"),
            on_click=on_tap,
            content=ft.Row(spacing=6, controls=[
                ft.Text(opcao, size=13, color=MUTED, weight=ft.FontWeight.W_500),
            ]),
        )
        chip_ref[0] = chip
        return chip

    for op in como_opcoes:
        como_chips.controls.append(make_chip(op))

    spin     = ft.ProgressRing(visible=False, color=PRIMARY, width=32, height=32, stroke_width=3)
    status_t = ft.Text("", color=MUTED, size=13, italic=True)
    send_ref = [None]
    dlg_ref  = [None]

    success_col = ft.Column(
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                width=80, height=80,
                gradient=ft.LinearGradient(colors=[SUCCESS, SUCCESS2], begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1)),
                border_radius=40,
                alignment=ft.Alignment(0,0),
                shadow=ft.BoxShadow(blur_radius=20, color="#4066BB6A", offset=ft.Offset(0,6)),
                content=ft.Icon(ft.Icons.CHECK, color="#FFFFFF", size=40),
            ),
            ft.Container(height=20),
            ft.Text("Pedido Confirmado! 🎉", size=24, weight=ft.FontWeight.W_800, color=DARK),
            ft.Container(height=8),
            ft.Text("Seu pagamento foi realizado com sucesso.", size=14, color=MUTED, text_align=ft.TextAlign.CENTER),
            ft.Text("Entraremos em contato pelo WhatsApp em breve 💝", size=14, color=MUTED, text_align=ft.TextAlign.CENTER),
            ft.Container(height=28),
            btn("← Voltar à Loja", lambda e: go_home(), PRIMARY, 260),
        ],
    )

    async def enviar(e):
        nm = nome_f.value.strip()
        tl = tel_f.value.strip()
        en = end_f.value.strip()
        dt = destinatario_f.value.strip()
        if not nm or not tl or not en or not dt:
            snack(page, "Preencha nome, WhatsApp, endereço e destinatário!", ok=False); return

        send_ref[0].visible = False
        spin.visible = True
        status_t.value = "Preparando sua cesta com amor... 🌸"
        page.update()

        payload = {
            "cesta_id":      cesta_id,
            "cesta_nome":    cesta_nome,
            "cesta_preco":   cesta_preco,
            "nome":          nm,
            "telefone":      tl,
            "endereco":      en,
            "cep":           cep_f.value.strip(),
            "data_entrega":  data_f.value.strip(),
            "periodo":       periodo_f.value.strip(),
            "destinatario":  dt,
            "msg_cartao":    msg_cartao_f.value.strip(),
            "como_soube":    como_soube_val[0],
            "observacoes":   obs_f.value.strip(),
            "usuario_id":    usuario.get("id", "") if usuario else "",
            "usuario_email": usuario.get("email", "") if usuario else "",
            "status":        "pendente",
            "criado_em":     datetime.now(timezone.utc).isoformat(),
        }
        res = await asyncio.to_thread(sb_post, "pedidos", payload, False)

        if not res:
            spin.visible = False; send_ref[0].visible = True; status_t.value = ""
            snack(page, "Erro ao salvar pedido. Tente novamente.", ok=False); return

        order_id = ""
        if isinstance(res, list) and res:
            order_id = str(res[0].get("id", ""))
        elif isinstance(res, dict):
            order_id = str(res.get("id", ""))

        status_t.value = "Gerando chave Pix... ✨"
        page.update()
        reset_payment_state()

        def _chamar_flask():
            sess = requests.Session()
            sess.headers.update({"Connection": "close"})
            try:
                resp = sess.post(
                    f"{URL_SERVER}/criar-sessao",  # APONTANDO PARA A NUVEM
                    json={
                        "cesta_nome":  cesta_nome,
                        "cesta_preco": cesta_preco,
                        "order_id":    order_id,
                        "payer_email": usuario.get("email", "comprador@gmail.com") if usuario else "comprador@gmail.com",
                    },
                    timeout=(5, 20),
                )
                return resp
            finally:
                sess.close()

        try:
            mp_res = await asyncio.to_thread(_chamar_flask)
            data   = mp_res.json()
        except Exception as ex:
            spin.visible = False; send_ref[0].visible = True; status_t.value = ""
            snack(page, f"Erro de conexão: {ex}", ok=False); return

        if "error" in data:
            spin.visible = False; send_ref[0].visible = True; status_t.value = ""
            snack(page, f"Erro no pagamento: {data['error']}", ok=False); return

        print("[DEBUG] Backend:", data)

        codigo_pix   = data.get("pix_code", "")
        qr_em_base64 = data.get("qr_code_base64", "")
        ticket_url   = (
            data.get("ticket_url", "")
            or data.get("point_of_interaction", {}).get("transaction_data", {}).get("ticket_url", "")
        )

        spin.visible = False; status_t.value = ""; page.update()

        # Gera QR localmente
        qr_img_path = ""
        try:
            import qrcode
            fonte = codigo_pix or ticket_url
            if fonte:
                qr = qrcode.QRCode(box_size=6, border=2)
                qr.add_data(fonte); qr.make(fit=True)
                img = qr.make_image(fill_color="#D81B60", back_color="white")
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                img.save(tmp.name); tmp.close()
                qr_img_path = tmp.name
                print(f"[DEBUG] QR salvo em: {qr_img_path}")
        except Exception as ex:
            print(f"[DEBUG] Erro QR: {ex}")

        def copiar_pix(e):
            try:
                page.set_clipboard(codigo_pix)
            except Exception:
                pass
            snack(page, "✅ Chave Pix copiada!")

        def copiar_link(e):
            try:
                page.set_clipboard(ticket_url)
            except Exception:
                pass
            snack(page, "✅ Link copiado!")

        def abrir_link(e):
            if ticket_url:
                page.launch_url(ticket_url)

        # ── Polling automático de pagamento ──────────────────────────────
        polling_ativo  = [True]
        poll_status_t  = ft.Text(
            "⏳ Aguardando confirmação do pagamento...",
            size=13, color=MUTED, text_align=ft.TextAlign.CENTER,
            weight=ft.FontWeight.W_600,
        )
        poll_ring = ft.ProgressRing(color=PRIMARY, width=22, height=22, stroke_width=3)

        async def _confirmar_pedido():
            """Marca pedido como confirmado no Supabase, envia Telegram e email."""
            if order_id:
                await asyncio.to_thread(
                    sb_patch, "pedidos", f"id=eq.{order_id}",
                    {"status": "confirmado"}, True
                )
                await asyncio.to_thread(
                    tg_send,
                    f"💸 <b>Pagamento Confirmado!</b>\n\n"
                    f"👤 {payload['nome']} | 📞 {payload['telefone']}\n"
                    f"🛒 {cesta_nome} — R$ {cesta_preco:.2f}\n"
                    f"📍 {payload['endereco']}\n"
                    f"🎁 Para: {payload['destinatario']}\n"
                    f"📅 Entrega: {payload['data_entrega']} ({payload['periodo']})\n"
                    f"💌 Cartão: {payload['msg_cartao']}\n"
                    f"📣 Soube por: {payload['como_soube']}"
                )
                # Envia email de confirmação para o cliente
                email_cliente = payload.get("usuario_email", "")
                if email_cliente:
                    await asyncio.to_thread(
                        send_email,
                        email_cliente,
                        cesta_nome,
                        cesta_preco,
                        payload.get("destinatario", ""),
                        payload.get("data_entrega", ""),
                        payload.get("periodo", ""),
                        payload.get("msg_cartao", ""),
                        payload.get("nome", ""),
                    )

        async def _fechar_dialog():
            polling_ativo[0] = False
            if dlg_ref[0] and dlg_ref[0] in page.overlay:
                try:
                    dlg_ref[0].open = False
                    page.overlay.remove(dlg_ref[0])
                except Exception:
                    pass
            if qr_img_path and os.path.exists(qr_img_path):
                try: os.remove(qr_img_path)
                except Exception: pass

        async def _poll_pagamento():
            """Consulta Flask a cada 4s até receber approved ou timeout (10min)."""
            tentativas = 0
            max_tentativas = 150  # 150 × 4s = 10 minutos
            while polling_ativo[0] and tentativas < max_tentativas:
                await asyncio.sleep(4)
                if not polling_ativo[0]:
                    break
                tentativas += 1
                try:
                    def _check():
                        s = requests.Session()
                        try:
                            # APONTANDO PARA A NUVEM AQUI TAMBÉM
                            r = s.get(
                                f"{URL_SERVER}/payment-status", 
                                timeout=5,
                            )
                            return r.json() if r.status_code == 200 else {}
                        except Exception as e:
                            print(f"[App] Erro de conexão com o servidor de pagamentos: {e}")
                            return {}
                        finally:
                            s.close()

                    estado = await asyncio.to_thread(_check)
                    status_pag = estado.get("status", "")
                    print(f"[POLL] tentativa={tentativas} status={status_pag!r}")

                    if status_pag == "approved":
                        # Pagamento confirmado automaticamente!
                        poll_status_t.value  = "✅ Pagamento confirmado!"
                        poll_status_t.color  = SUCCESS
                        poll_ring.visible    = False
                        try: page.update()
                        except Exception: pass
                        await asyncio.sleep(1.2)
                        await _confirmar_pedido()
                        await _fechar_dialog()
                        try:
                            go_confirmacao(
                                cesta_nome,
                                cesta_preco,
                                payload.get("destinatario", ""),
                            )
                        except Exception as ex:
                            print(f"[POLL] erro go_confirmacao: {ex}")
                        return

                    elif status_pag in ("rejected", "cancelled", "refunded"):
                        poll_status_t.value = "❌ Pagamento recusado ou cancelado."
                        poll_status_t.color = ERROR_C
                        poll_ring.visible   = False
                        try: page.update()
                        except Exception: pass
                        return

                except Exception as ex:
                    print(f"[POLL] erro: {ex}")

            # Timeout — mantém botão manual visível
            if polling_ativo[0]:
                poll_status_t.value = "⏰ Tempo esgotado. Use o botão abaixo."
                poll_ring.visible   = False
                try: page.update()
                except Exception: pass

        page.run_task(_poll_pagamento)

        async def fechar_e_confirmar(e):
            """Confirmação manual — caso o polling falhe."""
            await _fechar_dialog()
            spin.visible = True; status_t.value = "Confirmando seu pedido..."; page.update()
            await _confirmar_pedido()
            spin.visible = False; status_t.value = ""; page.update()
            go_confirmacao(cesta_nome, cesta_preco, payload.get("destinatario", ""))

        qr_widget = (
            ft.Image(src=qr_img_path, width=200, height=200, fit=ft.BoxFit.CONTAIN)
            if qr_img_path else
            ft.Icon(ft.Icons.QR_CODE_2, size=100, color=MUTED)
        )

        link_section = []
        if ticket_url:
            link_section = [
                ft.Container(height=10),
                ft.Container(
                    bgcolor="#F0F8FF", border_radius=12, padding=ft.Padding(14, 10, 14, 10),
                    border=ft.Border.all(1, BLUE),
                    content=ft.Column(spacing=8, controls=[
                        ft.Text("🔗 Link Mercado Pago", size=12, color=BLUE, weight=ft.FontWeight.W_700),
                        ft.Text(ticket_url, size=11, color=DARK, selectable=True),
                        ft.Row(spacing=8, controls=[
                            ft.Container(
                                bgcolor=BLUE, border_radius=10,
                                padding=ft.Padding(14, 8, 14, 8), on_click=copiar_link,
                                content=ft.Row(spacing=6, controls=[
                                    ft.Icon(ft.Icons.COPY, color="#FFF", size=14),
                                    ft.Text("Copiar", color="#FFF", size=12, weight=ft.FontWeight.W_600),
                                ]),
                            ),
                            ft.Container(
                                bgcolor=BLUE, border_radius=10,
                                padding=ft.Padding(14, 8, 14, 8), on_click=abrir_link,
                                content=ft.Row(spacing=6, controls=[
                                    ft.Icon(ft.Icons.OPEN_IN_NEW, color="#FFF", size=14),
                                    ft.Text("Abrir no App", color="#FFF", size=12, weight=ft.FontWeight.W_600),
                                ]),
                            ),
                        ]),
                    ]),
                ),
            ]

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(spacing=10, controls=[
                ft.Container(
                    width=36, height=36,
                    gradient=ft.LinearGradient(colors=[PRIMARY, ACCENT], begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1)),
                    border_radius=18,
                    alignment=ft.Alignment(0,0),
                    content=ft.Icon(ft.Icons.PIX, color="#FFFFFF", size=20),
                ),
                ft.Text("Pagamento via Pix 🌸", weight=ft.FontWeight.W_800, color=DARK, size=17),
            ]),
            content=ft.Column(
                tight=True, width=340,
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        "Aponte a câmera para o QR Code ou copie a chave abaixo:",
                        size=13, text_align=ft.TextAlign.CENTER, color=MUTED,
                    ),
                    ft.Container(height=12),
                    ft.Container(
                        alignment=ft.Alignment(0,0), width=224, height=224,
                        bgcolor="#FFFFFF", border_radius=20,
                        shadow=ft.BoxShadow(blur_radius=16, color="#20D81B60", offset=ft.Offset(0,4)),
                        border=ft.Border.all(2, "#F0D0DC"),
                        content=qr_widget,
                    ),
                    ft.Container(height=14),
                    ft.TextField(
                        value=codigo_pix, read_only=True,
                        label="Pix Copia e Cola",
                        multiline=True, max_lines=3,
                        border_color=PRIMARY, text_size=11, border_radius=12,
                        bgcolor="#FFF8FB",
                    ),
                    ft.Container(height=10),
                    ft.Container(
                        bgcolor=PRIMARY, border_radius=22,
                        width=310, height=46,
                        alignment=ft.Alignment(0,0),
                        on_click=copiar_pix,
                        shadow=ft.BoxShadow(blur_radius=10, color="#40D81B60", offset=ft.Offset(0,3)),
                        content=ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8, controls=[
                            ft.Icon(ft.Icons.COPY, color="#FFF", size=18),
                            ft.Text("Copiar Chave Pix", color="#FFF", weight=ft.FontWeight.W_700, size=14),
                        ]),
                    ),
                    *link_section,
                    ft.Container(height=8),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=10,
                        controls=[poll_ring, poll_status_t],
                    ),
                ],
            ),
            actions=[
                ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Text(
                            "Pix pago? Se não confirmou automaticamente:",
                            size=11, color=MUTED, text_align=ft.TextAlign.CENTER,
                        ),
                        ft.Container(
                            bgcolor=SUCCESS, border_radius=22,
                            width=310, height=50,
                            alignment=ft.Alignment(0,0),
                            on_click=lambda e: page.run_task(fechar_e_confirmar, e),
                            shadow=ft.BoxShadow(blur_radius=12, color="#4066BB6A", offset=ft.Offset(0,4)),
                            content=ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=10, controls=[
                                ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, color="#FFF", size=22),
                                ft.Text("Confirmar manualmente", color="#FFF", weight=ft.FontWeight.W_700, size=15),
                            ]),
                        ),
                    ],
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.CENTER,
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        dlg_ref[0] = dlg
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    send_btn = ft.Container(
        width=340, height=56,
        gradient=ft.LinearGradient(begin=ft.Alignment(-1,0), end=ft.Alignment(1,0), colors=[PRIMARY, PRIMARY2]),
        border_radius=28,
        alignment=ft.Alignment(0,0),
        on_click=lambda e: page.run_task(enviar, e),
        shadow=ft.BoxShadow(blur_radius=20, spread_radius=-2, color="#60D81B60", offset=ft.Offset(0, 6)),
        content=ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=10, controls=[
            ft.Icon(ft.Icons.PIX, color="#FFFFFF", size=22),
            ft.Text("Pagar com Pix Agora", color="#FFFFFF", weight=ft.FontWeight.W_800, size=17),
        ]),
    )
    send_ref[0] = send_btn

    def _card(title, emoji, controls):
        return ft.Container(
            bgcolor=CARD_BG, border_radius=20,
            padding=ft.Padding(20, 18, 20, 20),
            shadow=ft.BoxShadow(blur_radius=16, spread_radius=-2, color="#14D81B60", offset=ft.Offset(0,5)),
            content=ft.Column(spacing=14, controls=[
                ft.Row(spacing=8, controls=[
                    ft.Text(emoji, size=18),
                    ft.Text(title, size=15, weight=ft.FontWeight.W_700, color=DARK),
                ]),
                divider(),
                *controls,
            ]),
        )

    return ft.View(
        route="/pedido",
        bgcolor=BG,
        scroll=ft.ScrollMode.AUTO,
        appbar=ft.AppBar(
            title=ft.Text("Finalizar Pedido", color=DARK, weight=ft.FontWeight.W_800, size=18),
            bgcolor=CARD_BG, elevation=2, shadow_color="#10D81B60",
            leading=ft.Container(
                padding=ft.Padding(8, 0, 8, 0), on_click=lambda e: go_home(),
                content=ft.Icon(ft.Icons.ARROW_BACK_IOS_NEW, color=PRIMARY, size=20),
            ),
        ),
        controls=[
            ft.Container(
                padding=ft.Padding(20, 16, 20, 40),
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=16,
                    controls=[
                        # Hero da cesta
                        ft.Container(
                            width=400,
                            gradient=ft.LinearGradient(
                                begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1),
                                colors=[PRIMARY, ACCENT],
                            ),
                            border_radius=20,
                            padding=ft.Padding(20, 18, 20, 18),
                            shadow=ft.BoxShadow(blur_radius=20, color="#50D81B60", offset=ft.Offset(0,6)),
                            content=ft.Column(horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4, controls=[
                                ft.Text("🛍️ " + cesta_nome, size=20, weight=ft.FontWeight.W_900, color="#FFFFFF"),
                                ft.Text(f"R$ {cesta_preco:.2f}", size=28, weight=ft.FontWeight.W_900, color="#FFFFFF"),
                            ]),
                        ),

                        # Card 1: Seus dados
                        _card("Seus dados", "👤", [
                            nome_f,
                            ft.Container(height=4),
                            tel_f,
                        ]),

                        # Card 2: Entrega
                        _card("Entrega", "📦", [
                            end_f,
                            ft.Container(height=4),
                            ft.Row(spacing=10, controls=[cep_f, ft.Container(expand=True)]),
                            ft.Container(height=4),
                            data_f,
                            ft.Container(height=4),
                            periodo_f,
                            ft.Container(height=4),
                            destinatario_f,
                        ]),

                        # Card 3: Mensagem
                        _card("Mensagem especial", "💌", [
                            msg_cartao_f,
                        ]),

                        # Card 4: Observações
                        _card("Observações", "📝", [
                            obs_f,
                        ]),

                        # Card 5: Como soube
                        _card("Como nos conheceu?", "💬", [
                            ft.Text("Selecione uma opção:", size=13, color=MUTED),
                            como_chips,
                        ]),

                        ft.Container(height=8),
                        send_btn,
                        ft.Container(height=8),
                        ft.Row(alignment=ft.MainAxisAlignment.CENTER, controls=[spin]),
                        status_t,
                        success_col,
                    ],
                ),
            ),
        ],
    )



# ── CONFIRMAÇÃO VIEW ──────────────────────────────────────────────────────────
def confirmacao_view(page, cesta_nome, cesta_preco, destinatario, go_home):
    return ft.View(
        route="/confirmacao",
        bgcolor=BG,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Container(
                expand=True,
                padding=ft.Padding(28, 60, 28, 40),
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=0,
                    controls=[
                        # Ícone de sucesso animado
                        ft.Container(
                            width=110, height=110,
                            gradient=ft.LinearGradient(
                                colors=[SUCCESS, SUCCESS2],
                                begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1),
                            ),
                            border_radius=55,
                            alignment=ft.Alignment(0, 0),
                            shadow=ft.BoxShadow(blur_radius=30, color="#5066BB6A", offset=ft.Offset(0, 8)),
                            animate=ft.Animation(600, ft.AnimationCurve.BOUNCE_OUT),
                            content=ft.Icon(ft.Icons.CHECK_ROUNDED, color="#FFFFFF", size=56),
                        ),
                        ft.Container(height=32),

                        ft.Text("Pagamento Confirmado!", size=28, weight=ft.FontWeight.W_900, color=DARK,
                                text_align=ft.TextAlign.CENTER),
                        ft.Container(height=8),
                        ft.Text("Obrigada pela sua compra! 💝", size=16, color=MUTED,
                                text_align=ft.TextAlign.CENTER),
                        ft.Container(height=32),

                        # Card de detalhes
                        ft.Container(
                            width=380,
                            bgcolor=CARD_BG,
                            border_radius=24,
                            padding=ft.Padding(28, 24, 28, 24),
                            shadow=ft.BoxShadow(blur_radius=24, spread_radius=-4, color="#20D81B60", offset=ft.Offset(0, 8)),
                            content=ft.Column(
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=16,
                                controls=[
                                    ft.Container(
                                        width=60, height=60,
                                        gradient=ft.LinearGradient(
                                            colors=[PRIMARY, ACCENT],
                                            begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1),
                                        ),
                                        border_radius=30,
                                        alignment=ft.Alignment(0,0),
                                        content=ft.Text("🌸", size=30),
                                    ),
                                    ft.Text("Laços & Abraços", size=18, weight=ft.FontWeight.W_800, color=PRIMARY),
                                    ft.Container(height=1, bgcolor="#F0D0DC", width=300),
                                    ft.Column(spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
                                        ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8, controls=[
                                            ft.Icon(ft.Icons.CARD_GIFTCARD, color=PRIMARY, size=18),
                                            ft.Text(cesta_nome, size=15, weight=ft.FontWeight.W_700, color=DARK),
                                        ]),
                                        ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8, controls=[
                                            ft.Icon(ft.Icons.ATTACH_MONEY, color=SUCCESS, size=18),
                                            ft.Text(f"R$ {cesta_preco:.2f}", size=18, weight=ft.FontWeight.W_900, color=SUCCESS),
                                        ]),
                                        ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=8, controls=[
                                            ft.Icon(ft.Icons.PERSON_OUTLINE, color=MUTED, size=16),
                                            ft.Text(f"Para: {destinatario}", size=13, color=MUTED),
                                        ]) if destinatario else ft.Container(height=0),
                                    ]),
                                    ft.Container(height=1, bgcolor="#F0D0DC", width=300),
                                    ft.Container(
                                        bgcolor="#F0FFF4",
                                        border_radius=14,
                                        padding=ft.Padding(16, 12, 16, 12),
                                        width=320,
                                        content=ft.Column(spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
                                            ft.Text("✅ Pedido registrado com sucesso", size=13, color=SUCCESS, weight=ft.FontWeight.W_700, text_align=ft.TextAlign.CENTER),
                                            ft.Text("Entraremos em contato pelo WhatsApp\npara confirmar os detalhes da entrega.",
                                                    size=12, color=MUTED, text_align=ft.TextAlign.CENTER),
                                        ]),
                                    ),
                                ],
                            ),
                        ),
                        ft.Container(height=32),

                        # Mensagem emocional
                        ft.Container(
                            width=360,
                            bgcolor="#FFF5F9",
                            border_radius=18,
                            padding=ft.Padding(20, 16, 20, 16),
                            content=ft.Text(
                                "Cada cesta é preparada com muito carinho e dedicação.\n"
                                "Sua escolha vai fazer alguém muito especial sorrir hoje. 🌷",
                                size=13, color=MUTED, text_align=ft.TextAlign.CENTER, italic=True,
                            ),
                        ),
                        ft.Container(height=36),

                        # Botão voltar
                        ft.Container(
                            width=320, height=54,
                            gradient=ft.LinearGradient(
                                begin=ft.Alignment(-1, 0), end=ft.Alignment(1, 0),
                                colors=[PRIMARY, PRIMARY2],
                            ),
                            border_radius=27,
                            alignment=ft.Alignment(0, 0),
                            on_click=lambda e: go_home(),
                            shadow=ft.BoxShadow(blur_radius=16, color="#50D81B60", offset=ft.Offset(0, 5)),
                            content=ft.Row(alignment=ft.MainAxisAlignment.CENTER, spacing=10, controls=[
                                ft.Icon(ft.Icons.STOREFRONT_OUTLINED, color="#FFF", size=20),
                                ft.Text("Voltar à Loja", color="#FFFFFF", weight=ft.FontWeight.W_800, size=16),
                            ]),
                        ),
                        ft.Container(height=16),
                        ft.Text("Laços & Abraços • Feito com 💝", size=11, color=MUTED),
                    ],
                ),
            ),
        ],
    )

# ── ADMIN VIEW ────────────────────────────────────────────────────────────────
def admin_view(page, go_home):
    tab_index = [0]
    pedidos_content = _tab_pedidos(page)
    cestas_content  = _tab_cestas(page)
    pedidos_content.visible = True
    cestas_content.visible  = False

    tab_labels = ["📦 Pedidos", "🌷 Cestas"]

    def make_tab_btn(label, idx):
        active = idx == tab_index[0]
        return ft.Container(
            expand=True, height=46,
            gradient=ft.LinearGradient(colors=[PRIMARY, PRIMARY2], begin=ft.Alignment(-1,0), end=ft.Alignment(1,0))
            if active else None,
            bgcolor=None if active else CARD_BG,
            border_radius=14,
            alignment=ft.Alignment(0,0),
            on_click=lambda e, i=idx: switch_tab(i),
            shadow=ft.BoxShadow(blur_radius=8, color="#30D81B60", offset=ft.Offset(0,3)) if active else None,
            border=ft.Border.all(1, "#E8D0DA") if not active else None,
            content=ft.Text(
                label,
                color="#FFFFFF" if active else MUTED,
                weight=ft.FontWeight.W_700 if active else ft.FontWeight.W_500,
                size=14,
            ),
        )

    tab_btns = [make_tab_btn(l, i) for i, l in enumerate(tab_labels)]
    tab_row  = ft.Row(spacing=10, controls=tab_btns)

    def switch_tab(idx):
        tab_index[0] = idx
        pedidos_content.visible = idx == 0
        cestas_content.visible  = idx == 1
        for i in range(len(tab_labels)):
            active = i == idx
            tab_btns[i].gradient  = ft.LinearGradient(colors=[PRIMARY, PRIMARY2], begin=ft.Alignment(-1,0), end=ft.Alignment(1,0)) if active else None
            tab_btns[i].bgcolor   = None if active else CARD_BG
            tab_btns[i].shadow    = ft.BoxShadow(blur_radius=8, color="#30D81B60", offset=ft.Offset(0,3)) if active else None
            tab_btns[i].border    = None if active else ft.Border.all(1, "#E8D0DA")
            tab_btns[i].content.color  = "#FFFFFF" if active else MUTED
            tab_btns[i].content.weight = ft.FontWeight.W_700 if active else ft.FontWeight.W_500
        page.update()

    return ft.View(
        route="/admin",
        bgcolor=BG,
        expand=True,
        appbar=ft.AppBar(
            title=ft.Text("🌸 Gestão da Loja", color=DARK, weight=ft.FontWeight.W_800, size=18),
            bgcolor=CARD_BG, elevation=2, shadow_color="#10D81B60",
            leading=ft.Container(
                padding=ft.Padding(8,0,8,0), on_click=lambda e: go_home(),
                content=ft.Icon(ft.Icons.ARROW_BACK_IOS_NEW, color=PRIMARY, size=20),
            ),
        ),
        controls=[
            ft.Container(
                expand=True,
                padding=ft.Padding(16, 16, 16, 16),
                content=ft.Column(expand=True, controls=[
                    tab_row,
                    ft.Container(height=14),
                    pedidos_content,
                    cestas_content,
                ]),
            ),
        ],
    )


# ── TAB PEDIDOS ───────────────────────────────────────────────────────────────
def _tab_pedidos(page):
    col  = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)
    spin = ft.ProgressRing(color=PRIMARY, width=34, height=34, stroke_width=3)
    note = ft.Text("", color=MUTED, size=14)

    STATUS_META = {
        "pendente":             ("#FF9800", ft.Icons.HOURGLASS_EMPTY),
        "aguardando_pagamento": (BLUE,      ft.Icons.PAYMENT),
        "confirmado":           (SUCCESS,   ft.Icons.CHECK_CIRCLE),
        "cancelado":            (ERROR_C,   ft.Icons.CANCEL),
    }

    async def refresh(e=None):
        col.controls = [spin]; note.value = ""; page.update()
        pedidos = await asyncio.to_thread(sb_get, "pedidos", "order=criado_em.desc", True)
        col.controls = []
        if not pedidos:
            note.value = "Nenhum pedido ainda 🌷"; page.update(); return

        for p in pedidos:
            status   = p.get("status", "pendente")
            chip_clr, chip_icon = STATUS_META.get(status, (MUTED, ft.Icons.CIRCLE))

            async def mark(ev, pid=p.get("id"), s="confirmado"):
                await asyncio.to_thread(sb_patch, "pedidos", f"id=eq.{pid}", {"status": s}, True)
                await refresh()

            acoes = ft.Container()
            if status in ("pendente", "aguardando_pagamento"):
                acoes = ft.Row(spacing=8, controls=[
                    ft.Container(
                        bgcolor=SUCCESS, border_radius=10, padding=ft.Padding(14,7,14,7),
                        on_click=lambda ev, pid=p.get("id"): page.run_task(mark, ev, pid, "confirmado"),
                        content=ft.Row(spacing=6, controls=[
                            ft.Icon(ft.Icons.CHECK, color="#FFF", size=14),
                            ft.Text("Confirmar", color="#FFF", size=12, weight=ft.FontWeight.W_700),
                        ]),
                    ),
                    ft.Container(
                        bgcolor=ERROR_C, border_radius=10, padding=ft.Padding(14,7,14,7),
                        on_click=lambda ev, pid=p.get("id"): page.run_task(mark, ev, pid, "cancelado"),
                        content=ft.Row(spacing=6, controls=[
                            ft.Icon(ft.Icons.CLOSE, color="#FFF", size=14),
                            ft.Text("Cancelar", color="#FFF", size=12, weight=ft.FontWeight.W_700),
                        ]),
                    ),
                ])

            col.controls.append(
                ft.Container(
                    bgcolor=CARD_BG, border_radius=18,
                    padding=ft.Padding(16, 14, 16, 14),
                    shadow=ft.BoxShadow(blur_radius=14, spread_radius=-2, color="#12D81B60", offset=ft.Offset(0,4)),
                    content=ft.Column(spacing=8, controls=[
                        ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                            ft.Text(p.get("nome","—"), weight=ft.FontWeight.W_800, size=16, color=DARK),
                            ft.Container(
                                bgcolor=chip_clr, border_radius=20,
                                padding=ft.Padding(12,4,12,4),
                                content=ft.Row(spacing=4, controls=[
                                    ft.Icon(chip_icon, color="#FFF", size=12),
                                    ft.Text(status.replace("_"," ").upper(), color="#FFF", size=10, weight=ft.FontWeight.W_700),
                                ]),
                            ),
                        ]),
                        ft.Text(f"📞 {p.get('telefone','—')}  •  🛒 {p.get('cesta_nome','—')} — R$ {float(p.get('cesta_preco') or 0):.2f}", size=13, color=MUTED),
                        ft.Text(f"📍 {p.get('endereco','—')}", size=12, color=MUTED),
                        ft.Text(f"🎁 Para: {p.get('destinatario','—')}", size=12, color=MUTED) if p.get("destinatario") else ft.Container(height=0),
                        ft.Text(f"📅 {p.get('data_entrega','—')} ({p.get('periodo','—')})", size=12, color=MUTED) if p.get("data_entrega") else ft.Container(height=0),
                        ft.Text(f"💌 {p.get('msg_cartao','')}", size=12, color=DARK, italic=True) if p.get("msg_cartao") else ft.Container(height=0),
                        ft.Text(f"📣 Soube por: {p.get('como_soube','')}", size=11, color=MUTED) if p.get("como_soube") else ft.Container(height=0),
                        ft.Text(f"🕐 {str(p.get('criado_em',''))[:16].replace('T',' ')}", size=11, color="#C0A0B0"),
                        ft.Container(height=2),
                        acoes,
                    ]),
                )
            )
        page.update()

    page.run_task(refresh)

    return ft.Container(
        expand=True,
        content=ft.Column(expand=True, controls=[
            ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                note,
                ft.Container(
                    gradient=ft.LinearGradient(colors=[PRIMARY, PRIMARY2], begin=ft.Alignment(-1,0), end=ft.Alignment(1,0)),
                    border_radius=12, padding=ft.Padding(16,8,16,8),
                    on_click=lambda e: page.run_task(refresh),
                    shadow=ft.BoxShadow(blur_radius=8, color="#30D81B60", offset=ft.Offset(0,3)),
                    content=ft.Row(spacing=6, controls=[
                        ft.Icon(ft.Icons.REFRESH, color="#FFF", size=16),
                        ft.Text("Atualizar", color="#FFF", size=13, weight=ft.FontWeight.W_700),
                    ]),
                ),
            ]),
            ft.Container(height=10),
            col,
        ]),
    )


# ── TAB CESTAS ────────────────────────────────────────────────────────────────
def _tab_cestas(page):
    col  = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)
    spin = ft.ProgressRing(color=PRIMARY, width=34, height=34, stroke_width=3)

    nome_f    = mk_field("Nome da Cesta", width=340)
    preco_f   = mk_field("Preço (ex: 149.90)", width=160, kb=ft.KeyboardType.NUMBER)
    itens_f   = mk_field("Itens inclusos", multiline=True, max_lines=3, width=340)
    img_url_f = mk_field("URL da Imagem (opcional)", width=340, icon=ft.Icons.IMAGE_OUTLINED)
    emoji_f   = mk_field("Emoji", width=110)
    cor_f     = mk_field("Cor Hex", width=200, hint="#D81B60")
    edit_id   = [None]

    salvar_btn_text = ft.Text("💾 Salvar Cesta", color="#FFFFFF", weight=ft.FontWeight.W_700, size=14)
    salvar_btn_spin = ft.ProgressRing(color="#FFFFFF", width=20, height=20, stroke_width=2, visible=False)

    salvar_btn = ft.Container(
        width=180, height=46,
        gradient=ft.LinearGradient(colors=[PRIMARY, PRIMARY2], begin=ft.Alignment(-1,0), end=ft.Alignment(1,0)),
        border_radius=23, alignment=ft.Alignment(0,0),
        shadow=ft.BoxShadow(blur_radius=10, color="#40D81B60", offset=ft.Offset(0,3)),
        content=ft.Stack(controls=[
            ft.Container(alignment=ft.Alignment(0,0), content=salvar_btn_text),
            ft.Container(alignment=ft.Alignment(0,0), content=salvar_btn_spin),
        ]),
    )

    def clear_form():
        for f in (nome_f, preco_f, itens_f, emoji_f, img_url_f):
            f.value = ""
        cor_f.value   = PRIMARY
        emoji_f.value = "🧺"
        edit_id[0]    = None
        salvar_btn_text.value = "💾 Salvar Cesta"
        page.update()

    async def refresh(e=None):
        col.controls = [spin]; page.update()
        cestas = await asyncio.to_thread(sb_get, "cestas", "order=created_at.asc", True)
        col.controls = []

        for c in cestas:
            def edit(ev, cc=c):
                nome_f.value    = cc.get("nome", "")
                preco_f.value   = str(cc.get("preco", ""))
                itens_f.value   = cc.get("itens", "")
                emoji_f.value   = cc.get("emoji", "🧺")
                cor_f.value     = cc.get("cor", PRIMARY)
                img_url_f.value = cc.get("imagem_url", "")
                edit_id[0]      = cc.get("id")
                salvar_btn_text.value = "✏️ Atualizar Cesta"
                page.update()

            async def excluir(ev, cid=c.get("id")):
                await asyncio.to_thread(sb_delete, "cestas", f"id=eq.{cid}", True)
                await refresh()

            img_url = c.get("imagem_url", "")
            thumb = (
                ft.Image(src=img_url, width=54, height=54, fit=ft.BoxFit.COVER, border_radius=12)
                if img_url else
                ft.Container(
                    width=54, height=54, bgcolor=c.get("cor", PRIMARY), border_radius=12,
                    alignment=ft.Alignment(0,0), content=ft.Text(c.get("emoji","🧺"), size=26),
                )
            )

            col.controls.append(
                ft.Container(
                    bgcolor=CARD_BG, border_radius=16,
                    padding=ft.Padding(14, 12, 14, 12),
                    shadow=ft.BoxShadow(blur_radius=12, spread_radius=-2, color="#10D81B60", offset=ft.Offset(0,3)),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Row(spacing=14, controls=[
                                thumb,
                                ft.Column(spacing=3, controls=[
                                    ft.Text(c.get("nome",""), weight=ft.FontWeight.W_700, color=DARK, size=15),
                                    ft.Text(f"R$ {float(c.get('preco') or 0):.2f}", color=PRIMARY, size=13, weight=ft.FontWeight.W_700),
                                    ft.Text(str(c.get("itens",""))[:40]+"…" if len(str(c.get("itens","")))>40 else str(c.get("itens","")),
                                            size=11, color=MUTED),
                                ]),
                            ]),
                            ft.Row(spacing=4, controls=[
                                ft.Container(
                                    bgcolor="#FFF0F5", border_radius=10, padding=ft.Padding(8,8,8,8),
                                    on_click=edit,
                                    content=ft.Icon(ft.Icons.EDIT_OUTLINED, color=PRIMARY, size=18),
                                ),
                                ft.Container(
                                    bgcolor="#FFF0F0", border_radius=10, padding=ft.Padding(8,8,8,8),
                                    on_click=lambda ev, cid=c.get("id"): page.run_task(excluir, ev, cid),
                                    content=ft.Icon(ft.Icons.DELETE_OUTLINE, color=ERROR_C, size=18),
                                ),
                            ]),
                        ],
                    ),
                )
            )
        page.update()

    async def salvar(e):
        # Validações
        if not nome_f.value.strip():
            snack(page, "Nome da cesta é obrigatório!", ok=False); return
        try:
            preco = float(preco_f.value.replace(",", "."))
        except Exception:
            snack(page, "Preço inválido! Use ponto como separador (ex: 149.90)", ok=False); return

        # Loading
        salvar_btn_text.visible = False
        salvar_btn_spin.visible = True
        page.update()

        data = {
            "nome":       nome_f.value.strip(),
            "preco":      preco,
            "itens":      itens_f.value.strip(),
            "emoji":      emoji_f.value.strip() or "🧺",
            "cor":        cor_f.value.strip() or PRIMARY,
            "imagem_url": img_url_f.value.strip(),
        }

        if edit_id[0]:
            resultado = await asyncio.to_thread(sb_patch, "cestas", f"id=eq.{edit_id[0]}", data, True)
            salvar_btn_text.visible = True; salvar_btn_spin.visible = False
            if resultado is not None:
                snack(page, "Cesta atualizada com sucesso! 🌷")
            else:
                snack(page, "Erro ao atualizar. Verifique os dados.", ok=False); return
        else:
            resultado = await asyncio.to_thread(sb_post, "cestas", data, True)
            salvar_btn_text.visible = True; salvar_btn_spin.visible = False
            if resultado is not None:
                snack(page, "Nova cesta criada com sucesso! 🎉")
            else:
                snack(page, "Erro ao criar cesta. Verifique os dados e tente novamente.", ok=False); return

        clear_form()
        await refresh()

    salvar_btn.on_click = lambda e: page.run_task(salvar, e)
    page.run_task(refresh)

    return ft.Container(
        expand=True,
        content=ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=14, controls=[
            # Formulário
            ft.Container(
                bgcolor=CARD_BG, border_radius=20,
                padding=ft.Padding(20, 18, 20, 20),
                shadow=ft.BoxShadow(blur_radius=16, spread_radius=-2, color="#14D81B60", offset=ft.Offset(0,5)),
                content=ft.Column(spacing=12, controls=[
                    ft.Row(spacing=8, controls=[
                        ft.Container(
                            width=34, height=34,
                            gradient=ft.LinearGradient(colors=[PRIMARY, ACCENT], begin=ft.Alignment(-1,-1), end=ft.Alignment(1,1)),
                            border_radius=17, alignment=ft.Alignment(0,0),
                            content=ft.Icon(ft.Icons.ADD_BOX_OUTLINED, color="#FFF", size=18),
                        ),
                        ft.Text("Cadastrar / Editar Cesta", weight=ft.FontWeight.W_800, color=DARK, size=15),
                    ]),
                    divider(),
                    nome_f,
                    preco_f,
                    itens_f,
                    img_url_f,
                    ft.Text("💡 Use links de imagens públicas (ex: Imgur)", size=11, color=MUTED),
                    ft.Row(spacing=10, controls=[emoji_f, cor_f]),
                    ft.Container(height=4),
                    ft.Row(spacing=10, controls=[
                        salvar_btn,
                        ft.Container(
                            height=46, padding=ft.Padding(20, 0, 20, 0),
                            border_radius=23, bgcolor="#F5F5F5",
                            alignment=ft.Alignment(0,0),
                            border=ft.Border.all(1, "#E0E0E0"),
                            on_click=lambda e: clear_form(),
                            content=ft.Text("Limpar", color=MUTED, size=14, weight=ft.FontWeight.W_600),
                        ),
                    ]),
                ]),
            ),
            # Lista
            ft.Row(spacing=8, controls=[
                ft.Text("🌸", size=18),
                ft.Text("Cestas Cadastradas", weight=ft.FontWeight.W_800, color=DARK, size=15),
            ]),
            col,
        ]),
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main(page: ft.Page):
    # Desativado: o servidor de pagamentos agora roda isolado na nuvem do Render!
    # start_payment_server() 

    page.title      = "Laços & Abraços"
    page.bgcolor    = BG
    page.theme_mode = ft.ThemeMode.LIGHT
    page.scroll     = None
    page.fonts      = {}

    try:
        page.window.width  = 440
        page.window.height = 820
    except Exception:
        pass

    is_admin  = check_is_admin(page)
    usuario   = [None]   # guarda dados do usuário logado

    def go_home(e=None):
        page.views.clear()
        page.views.append(home_view(
            page,
            go_pedido=lambda cid, cn, cp: go_pedido(cid, cn, cp),
            go_admin=go_admin,
            is_admin=is_admin,
            usuario=usuario[0],
        ))
        page.update()

    def go_pedido(cid, cn, cp):
        page.views.append(pedido_view(
            page, cid, cn, cp,
            go_home=go_home,
            go_confirmacao=go_confirmacao,
            usuario=usuario[0],
        ))
        page.update()

    def go_confirmacao(cesta_nome, cesta_preco, destinatario):
        page.views.clear()
        page.views.append(confirmacao_view(page, cesta_nome, cesta_preco, destinatario, go_home=go_home))
        page.update()

    def go_admin(e=None):
        if not is_admin: return
        page.views.append(admin_view(page, go_home=go_home))
        page.update()

    def on_auth_success(user_data):
        usuario[0] = user_data
        go_home()

    # Começa na tela de auth
    page.views.append(auth_view(page, on_success=on_auth_success))
    page.update()


ft.run(main)