import os
import json
import time
import threading
import requests
from collections import deque
from flask import Flask, request, jsonify, render_template
import websocket

app = Flask(__name__)

# ==================== DERIV CLIENT ====================
class DerivClient:
    def __init__(self):
        self.token = os.environ.get('DERIV_TOKEN')
        self.ws = None
        self.connected = False
        self.authorized = False
        self.balance = 0
        self.subscribed_symbols = set()
        self.tick_handlers = []
        self.reconnect_delay = 5

    def set_token(self, token):
        self.token = token
        with open('token.txt', 'w') as f:
            f.write(token)

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get('msg_type') == 'authorize':
                self.authorized = True
                self.balance = data['authorize']['balance']
                print(f"✅ Autorizado. Saldo: {self.balance} USD")
                self.ws.send(json.dumps({"subscribe": "balance"}))
                for sym in self.subscribed_symbols:
                    self.ws.send(json.dumps({"ticks": sym}))
            elif data.get('msg_type') == 'balance':
                self.balance = data['balance']['balance']
            elif data.get('msg_type') == 'tick':
                tick = data['tick']
                for handler in self.tick_handlers:
                    handler(tick)
            elif 'error' in data:
                print(f"⚠️ Erro Deriv: {data['error']['message']}")
        except Exception as e:
            print(f"❌ Erro processando mensagem: {e}")

    def on_error(self, ws, error):
        print(f"❌ WebSocket error: {error}")
        self.connected = False
        self.authorized = False

    def on_close(self, ws, close_status_code, close_msg):
        print(f"🔌 Desconectado. Reconectando em {self.reconnect_delay}s...")
        self.connected = False
        self.authorized = False
        time.sleep(self.reconnect_delay)
        self.connect()

    def on_open(self, ws):
        print("✅ WebSocket conectado")
        self.connected = True
        if self.token:
            self.ws.send(json.dumps({"authorize": self.token}))
        else:
            print("⚠️ Nenhum token configurado")

    def connect(self):
        if self.connected:
            return
        ws_url = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
        self.ws = websocket.WebSocketApp(ws_url,
                                         on_open=self.on_open,
                                         on_message=self.on_message,
                                         on_error=self.on_error,
                                         on_close=self.on_close)
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            return
        self.subscribed_symbols.add(symbol)
        if self.connected and self.authorized:
            self.ws.send(json.dumps({"ticks": symbol}))
        print(f"📊 Subscrito a ticks: {symbol}")

    def add_tick_handler(self, handler):
        self.tick_handlers.append(handler)

deriv_client = DerivClient()

# ==================== DIGIT ANALYZER ====================
class DigitAnalyzer:
    def __init__(self, window_size=15, slow_threshold=2.5):
        self.window_size = window_size
        self.slow_threshold = slow_threshold
        self.digits = deque(maxlen=window_size)
        self.timestamps = deque(maxlen=window_size)

    def add_tick(self, price, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        digit = int(float(price)) % 10
        self.digits.append(digit)
        self.timestamps.append(timestamp)
        return digit

    def detect_slow_digit(self):
        if len(self.digits) < 5:
            return None, None
        intervals = [self.timestamps[i] - self.timestamps[i-1] for i in range(1, len(self.timestamps))]
        avg_interval = sum(intervals) / len(intervals)
        digit_gaps = {}
        for i in range(1, len(self.digits)):
            gap = self.timestamps[i] - self.timestamps[i-1]
            d = self.digits[i]
            digit_gaps.setdefault(d, []).append(gap)
        slow_digit = None
        max_gap = 0
        for d, gaps in digit_gaps.items():
            avg_gap = sum(gaps) / len(gaps)
            if avg_gap > max_gap and avg_gap > self.slow_threshold:
                max_gap = avg_gap
                slow_digit = d
        if slow_digit is not None:
            direction = "CALL" if slow_digit % 2 == 0 else "PUT"
            return slow_digit, direction
        return None, None

digit_analyzer = DigitAnalyzer()

# ==================== TRADING BOT ====================
class TradingBot:
    def __init__(self):
        self.active = False
        self.last_signal_time = 0
        self.cooldown = 10
        self.default_amount = 10.0
        self.default_duration = 5
        self.symbol = "R_100"

    def start(self):
        if self.active:
            return
        self.active = True
        deriv_client.add_tick_handler(self.on_tick)
        print("🤖 Bot de trading iniciado")

    def stop(self):
        self.active = False

    def on_tick(self, tick):
        if not self.active:
            return
        price = tick.get('quote', 0)
        symbol = tick.get('symbol', 'R_100')
        print(f"📈 Tick: {symbol} @ {price}")
        digit = digit_analyzer.add_tick(price)
        print(f"🔵 Dígito: {digit}")
        slow_digit, direction = digit_analyzer.detect_slow_digit()
        if slow_digit and direction:
            now = time.time()
            if now - self.last_signal_time >= self.cooldown:
                self.last_signal_time = now
                print(f"🎯 Sinal: dígito {slow_digit} -> {direction}")
                self.execute_trade(slow_digit, direction, price)
            else:
                print("⏸️ Cooldown")
        else:
            print("⏳ Sem sinal")

    def execute_trade(self, digit, direction, price):
        # Nesta versão standalone, chamamos a rota interna via requests
        payload = {
            "symbol": self.symbol,
            "amount": self.default_amount,
            "duration": self.default_duration,
            "duration_unit": "t",
            "contract_type": direction,
            "digit": digit,
            "current_price": price
        }
        try:
            resp = requests.post("http://127.0.0.1:10000/api/trade/digit", json=payload, timeout=5)
            if resp.status_code == 200:
                print("✅ Trade executado")
            else:
                print(f"❌ Trade falhou: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"❌ Erro: {e}")

trading_bot = TradingBot()

# ==================== ROTAS FLASK ====================
@app.route('/')
def index():
    # Pode retornar um HTML simples ou redirecionar
    return jsonify({"status": "Bot em execução", "endpoints": ["/api/status", "/api/trade/digit", "/api/auth/save_token"]})

@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({
        "connected": deriv_client.connected,
        "authorized": deriv_client.authorized,
        "balance": deriv_client.balance,
        "bot_active": trading_bot.active,
        "token_set": bool(deriv_client.token)
    })

@app.route('/api/auth/save_token', methods=['POST'])
def save_token():
    data = request.get_json()
    token = data.get('token')
    if not token:
        return jsonify({"error": "Token obrigatório"}), 400
    deriv_client.set_token(token)
    deriv_client.connect()
    # Aguarda um pouco e subscreve ticks
    time.sleep(2)
    if deriv_client.authorized:
        deriv_client.subscribe_ticks("R_100")
        trading_bot.start()
    return jsonify({"status": "ok"})

@app.route('/api/trade/digit', methods=['POST'])
def trade_digit():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON inválido"}), 400
    required = ["symbol", "amount", "duration", "duration_unit", "contract_type", "digit"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Falta {field}"}), 400
    # Aqui você implementaria o envio real para a Deriv (via WebSocket)
    # Por enquanto, apenas simula a ordem
    print(f"📝 Ordem recebida: {data}")
    # Envia para a Deriv via deriv_client (seria necessário método buy_contract)
    # Retornamos sucesso simulado
    return jsonify({"status": "order_placed", "simulated": True})

@app.route('/api/connect', methods=['POST'])
def connect():
    if not deriv_client.token:
        return jsonify({"error": "Sem token"}), 400
    deriv_client.connect()
    time.sleep(2)
    if deriv_client.authorized:
        deriv_client.subscribe_ticks("R_100")
        trading_bot.start()
    return jsonify({"status": "connecting"})

@app.route('/api/report', methods=['GET'])
def report():
    return jsonify({"trades": []})

@app.route('/api/martingale/status', methods=['GET'])
def martingale_status():
    return jsonify({"active": False})

# ==================== INICIALIZAÇÃO ====================
def initialize():
    if deriv_client.token:
        print("🔑 Token encontrado no ambiente. A conectar...")
        deriv_client.connect()
        # Espera a conexão e autorização
        for _ in range(10):
            if deriv_client.authorized:
                break
            time.sleep(1)
        if deriv_client.authorized:
            deriv_client.subscribe_ticks("R_100")
            trading_bot.start()
            print("✅ Bot iniciado automaticamente")
        else:
            print("⚠️ Falha na autorização")
    else:
        print("ℹ️ Nenhum token definido. Use POST /api/auth/save_token")

# Inicia a inicialização numa thread separada para não bloquear o Flask
threading.Thread(target=initialize, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
