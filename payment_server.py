import threading
import traceback
import os
from flask import Flask, jsonify, request
import mercadopago
from dotenv import load_dotenv

load_dotenv()

_flask = Flask(__name__)

ACCESS_TOKEN = os.getenv(
    "MP_ACCESS_TOKEN",
    "TEST-5848323176406288-051716-7cf9df4522c85ebf6585b78fbd04e202-1179794795"
)
print(f"[payment_server] Token carregado: {ACCESS_TOKEN[:20]}...")
sdk = mercadopago.SDK(ACCESS_TOKEN)

LOCAL_PORT = 8080

# Guarda o payment_id do pagamento atual para polling
_payment_state = {"status": "pending", "payment_id": None}


def get_payment_state():
    return _payment_state


def reset_payment_state():
    global _payment_state
    _payment_state = {"status": "pending", "payment_id": None}


@_flask.route("/criar-sessao", methods=["POST"])
def criar_sessao():
    try:
        dados = request.get_json()
        print(f"[payment_server] Dados recebidos: {dados}")

        valor       = float(dados.get("cesta_preco") or 0)
        nome_cesta  = dados.get("cesta_nome", "Laços & Abraços")
        order_id    = dados.get("order_id", "")
        payer_email = dados.get("payer_email") or "TESTUSER7506064733260436489@testuser.com"

        print(f"[payment_server] payer_email: {payer_email}")

        payment_data = {
            "transaction_amount": valor,
            "description":        nome_cesta,
            "payment_method_id":  "pix",
            "payer": {
                "email":      payer_email,
                "first_name": "Comprador",
                "last_name":  "Teste",
                "identification": {"type": "CPF", "number": "12345678909"},
            },
            "external_reference": str(order_id),
        }

        resposta_api = sdk.payment().create(payment_data)
        http_status  = resposta_api["status"]
        pagamento    = resposta_api["response"]

        print(f"[payment_server] MP status={http_status} | body={pagamento}")

        if http_status >= 400:
            msg = pagamento.get("message", "Erro desconhecido")
            return jsonify({"error": msg}), 400

        dados_tx   = pagamento["point_of_interaction"]["transaction_data"]
        pix_code   = dados_tx.get("qr_code", "")
        qr_base64  = dados_tx.get("qr_code_base64", "")
        payment_id = pagamento["id"]

        # ── Salva payment_id para o polling consultar ──────────────────
        _payment_state["status"]     = "pending"
        _payment_state["payment_id"] = payment_id

        print(f"[payment_server] PIX criado | payment_id={payment_id}")

        return jsonify({
            "status":         "pending",
            "pix_code":       pix_code,
            "qr_code_base64": qr_base64,
            "qr_code_image":  qr_base64,
            "payment_id":     payment_id,
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@_flask.route("/payment-status", methods=["GET"])
def payment_status():
    """
    Polling: consulta o Mercado Pago diretamente pelo payment_id salvo.
    Retorna {"status": "approved" | "pending" | "rejected" | ...}
    """
    payment_id = _payment_state.get("payment_id")

    if not payment_id:
        return jsonify({"status": "pending", "detail": "sem payment_id"}), 200

    try:
        res    = sdk.payment().get(payment_id)
        body   = res["response"]
        status = body.get("status", "pending")

        print(f"[payment_server] /payment-status id={payment_id} status={status}")

        # Atualiza estado interno
        _payment_state["status"] = status

        return jsonify({
            "status":     status,
            "payment_id": payment_id,
            "detail":     body.get("status_detail", ""),
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "pending", "error": str(e)}), 200


def start_payment_server():
    def _run():
        _flask.run(
            host="127.0.0.1",
            port=LOCAL_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    t = threading.Thread(target=_run, daemon=True, name="MercadoPagoFlask")
    t.start()
    print(f"[payment_server] Flask rodando em http://127.0.0.1:{LOCAL_PORT}")