import asyncio
import aiohttp
import time
import random
import string
from flask import Flask, request, render_template_string, jsonify

app = Flask(__name__)

# ========= HTML do painel ==========
HTML_FORM = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Painel</title>
  <style>
    body {
      background: #0c0c0c;
      color: #fff;
      font-family: Arial, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100vh;
    }
    .content {
      background: #1a1a1a;
      padding: 30px;
      border-radius: 12px;
      width: 350px;
    }
    input, button {
      width: 100%;
      padding: 10px;
      margin: 10px 0;
      border-radius: 8px;
      border: none;
      outline: none;
    }
    input { background: #2a2a2a; color: #fff; }
    button { background: #0d6efd; color: #fff; cursor: pointer; }
    button:hover { background: #0b5ed7; }
  </style>
</head>
<body>
  <div class="content">
    <form method="POST" action="/run">
      <h3>Nome do teste</h3>
      <input type="text" name="nome" placeholder="Digite o nome">

      <h3>Link alvo</h3>
      <input type="url" name="url" placeholder="https://exemplo.com">

      <h3>Total de requisições</h3>
      <input type="number" name="total" placeholder="1000">

      <h3>Concorrência</h3>
      <input type="number" name="concorrencia" placeholder="100">

      <button type="submit">Iniciar Teste</button>
    </form>
  </div>
</body>
</html>
"""

# ========= Funções de carga ==========
def generate_payload(size=100_000):
    return {
        "id": random.randint(1, 10_000_000),
        "data": ''.join(random.choices(string.ascii_letters + string.digits, k=size))
    }

async def make_request(session, url, idx):
    payload = generate_payload()
    try:
        async with session.post(
            url,
            json=payload,
            headers={"X-Test": "LoadTest", "Content-Type": "application/json"},
            ssl=False,
            timeout=10
        ) as response:
            return idx, response.status
    except Exception as e:
        return idx, f"Erro: {e}"

async def run_load_test(url, total, concorrencia):
    connector = aiohttp.TCPConnector(limit=concorrencia, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        start = time.time()
        semaphore = asyncio.Semaphore(concorrencia)

        async def bound_task(i):
            async with semaphore:
                return await make_request(session, url, i)

        tasks = [bound_task(i) for i in range(1, total + 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        duration = time.time() - start
        success = sum(1 for _, r in results if isinstance(r, int))
        fail = sum(1 for _, r in results if not isinstance(r, int))

        return {"tempo": duration, "sucesso": success, "falhas": fail}

# ========= Rotas Flask ==========
@app.route("/")
def index():
    return render_template_string(HTML_FORM)

@app.route("/run", methods=["POST"])
def run_test():
    nome = request.form.get("nome")
    url = request.form.get("url")
    total = int(request.form.get("total", 100))
    concorrencia = int(request.form.get("concorrencia", 10))

    # roda o teste async
    result = asyncio.run(run_load_test(url, total, concorrencia))

    return jsonify({
        "nome": nome,
        "url": url,
        "resultado": result
    })

if __name__ == "__main__":
    app.run(debug=True)
