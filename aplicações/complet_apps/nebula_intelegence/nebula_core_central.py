import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# Caminho base do RAG
BASE_PATH = "bigdata"

def carregar_base():
    """Carrega todas as perguntas e respostas dos slots"""
    dados = []
    for slot in range(1, 5):  # 4 slots
        slot_path = os.path.join(BASE_PATH, f"slot{slot}", "versionamento1")
        if os.path.exists(slot_path):
            for arquivo in os.listdir(slot_path):
                if arquivo.endswith(".json"):
                    with open(os.path.join(slot_path, arquivo), "r", encoding="utf-8") as f:
                        try:
                            dados.extend(json.load(f))  # cada arquivo é uma lista de {pergunta, resposta}
                        except Exception as e:
                            print(f"Erro ao carregar {arquivo}: {e}")
    return dados

# Carrega tudo na memória ao iniciar
BASE_CONHECIMENTO = carregar_base()

# Palavras associadas ao slot 1
palavras_slot1 = [
    "estudo", "matemática", "fisica", "química", "história", "geografia",
    "português", "literatura", "biologia", "escola", "prova", "enem",
    "vestibular", "faculdade", "ciência", "livro", "apostila", "exercício",
    "curso", "professor", "universidade", "educação", "redação"
]

def procurar_resposta(mensagem):
    """Procura resposta na base de conhecimento"""
    mensagem_lower = mensagem.lower()
    for item in BASE_CONHECIMENTO:
        if mensagem_lower in item.get("pergunta", "").lower():
            return item.get("resposta")
    return "Desculpe, não encontrei uma resposta para isso."

@app.route('/mensagem', methods=['POST'])
def mensagem():
    data = request.get_json()
    mensagem = data.get("mensagem")

    if not mensagem:
        return jsonify({"erro": "Nenhuma mensagem recebida"}), 400

    # Checa se a mensagem é do slot1
    if any(p in mensagem.lower() for p in palavras_slot1):
        chance = "0% - 35%"
        resposta = procurar_resposta(mensagem)
        return jsonify({"resposta": resposta, "slot": "1", "chance": chance}), 200

    # Caso geral
    resposta = procurar_resposta(mensagem)
    return jsonify({"resposta": resposta}), 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
