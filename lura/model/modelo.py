from flask import Flask, render_template, request, redirect, url_for, session, flash
import torch
import os
import sys
import argparse 
import time
import torch.nn as nn
import torch.optim as optim 

# --- AJUSTE DE CAMINHO E IMPORTAÇÕES ---

# Garante que as importações de model e utils funcionem
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

try:
    # Importa componentes essenciais para ambas as funcionalidades
    from model.inferencia import prever_resposta, modelo 
    from model.modelo import ModeloLeve
    from utils.tokenizer import Tokenizer 
except ImportError as e:
    print(f"ERRO CRÍTICO DE IMPORTAÇÃO: {e}")
    print("Verifique se model/inferencia.py, model/modelo.py e utils/tokenizer.py existem e se a estrutura de pastas está correta.")
    sys.exit(1)


app = Flask(__name__)
app.secret_key = 'uma_chave_secreta_forte_para_sessao' 

# --- ROTAS FLASK (INFERÊNCIA/FEEDBACK) ---

@app.route("/", methods=["GET", "POST"])
def index():
    chat = session.get('chat', [])
    feedback_status = session.pop('feedback_status', None)

    if request.method == "POST":
        pergunta = request.form.get("pergunta")
        
        if pergunta:
            try:
                resposta, alvo_tensor, entrada_tensor = prever_resposta(chat, pergunta)
                
                # Serialização para sessão (Tensors -> Tipos Nativos)
                entrada_list = entrada_tensor.cpu().tolist()
                alvo_int = alvo_tensor.item()
                
            except Exception as e:
                resposta = f"🤖 Desculpe, houve um erro na inferência: {e}"
                entrada_list = [[0]]
                alvo_int = 0
            
            chat.append({
                "pergunta": pergunta,
                "resposta": resposta,
                "entrada": entrada_list,
                "alvo": alvo_int        
            })
            
            if len(chat) > 10:
                session['chat'] = []
                flash("✅ O histórico da conversa foi limpo para iniciar um novo ciclo de aprendizado (Máx. 10).")
                return redirect(url_for("index"))

        session['chat'] = chat
        return redirect(url_for("index"))

    return render_template("index.html", chat=chat, feedback_status=feedback_status)


@app.route("/feedback/<int:idx>/<tipo>", methods=["POST"])
def feedback(idx, tipo):
    chat = session.get('chat', [])
    
    if 0 <= idx < len(chat):
        item = chat[idx]
        
        try:
            # Desserialização (Tipos Nativos -> Tensors)
            entrada_tensor = torch.tensor(item["entrada"])
            alvo_tensor = torch.tensor([item["alvo"]]).long()
            
        except TypeError as e:
            session['feedback_status'] = f"❌ Erro de conversão de dados: {e}. Histórico limpo."
            session['chat'] = []
            return redirect(url_for("index"))

        positivo = (tipo == "positivo")
        
        try:
            # Chama o método de ajuste do modelo (RLHF)
            msg = modelo.feedback(entrada_tensor, alvo_tensor.flatten(), positivo=positivo)
            
            session['chat'] = []
            session['feedback_status'] = msg 
            
        except Exception as e:
             session['feedback_status'] = f"❌ Erro de Ajuste (PyTorch): {e}. Histórico limpo."
             session['chat'] = [] 

    else:
        session['feedback_status'] = "⚠️ Índice de chat inválido para feedback."

    return redirect(url_for("index"))

# --- FUNÇÃO DE TREINAMENTO AUTOMÁTICO ---

def run_training():
    
    # Paths configurados para rodar de lura.py (na raiz)
    CAMINHO_TEXTOS = "data/textos.txt"
    CAMINHO_PESOS = "model/modelo_treinado.pth" 
    NUM_EPOCAS = 10 
    LR_TREINAMENTO = 0.001
    SEQUENCE_LENGTH = 50 

    print("--- Inicializando o Treinamento Não-Supervisionado ---")
    
    # 1. Inicializa tokenizer
    tokenizer = Tokenizer()
    try:
        with open(CAMINHO_TEXTOS, "r", encoding="utf-8") as f:
            textos_completos = f.read()
        tokenizer.build_vocab([textos_completos])
    except FileNotFoundError:
        print(f"ERRO: Arquivo {CAMINHO_TEXTOS} não encontrado. Abortando.")
        sys.exit(1)

    # 2. Inicializa o modelo
    modelo_treino = ModeloLeve(vocab_size=tokenizer.vocab_size) 
    device = modelo_treino.device
    modelo_treino.train() 
    
    # 3. Preparação dos Dados (Tokens)
    todos_tokens = tokenizer.texto_para_tokens(textos_completos)
    todos_tokens_tensor = torch.tensor(todos_tokens, dtype=torch.long)

    # Otimizador e Função de Perda
    otimizador = optim.Adam(modelo_treino.parameters(), lr=LR_TREINAMENTO)
    criterio = nn.CrossEntropyLoss() 

    # --- Loop de Treinamento ---
    t_inicio = time.time()
    num_passos_total = 0

    print(f"Vocabulário de tamanho: {tokenizer.vocab_size}")
    print(f"Total de tokens para treino: {len(todos_tokens)}")

    for epoca in range(1, NUM_EPOCAS + 1):
        h = None 
        perda_acumulada = 0
        num_lotes = 0

        for i in range(0, len(todos_tokens_tensor) - SEQUENCE_LENGTH - 1, SEQUENCE_LENGTH):
            entrada_tokens = todos_tokens_tensor[i : i + SEQUENCE_LENGTH].unsqueeze(0).to(device)
            alvo_tokens = todos_tokens_tensor[i + 1 : i + SEQUENCE_LENGTH + 1].to(device)
            
            # Forward Pass (usa o método forward para treino em massa)
            saida_logits, h = modelo_treino(entrada_tokens, h)
            
            # ❗ CORREÇÃO CRUCIAL: Detach o estado oculto para evitar Runtime Error (TBPTT)
            # Garante que h seja um tensor antes de chamar detach()
            if isinstance(h, torch.Tensor):
                h = h.detach()
            
            # Cálculo da Perda (Saida_logits já está no formato (N, V) e alvo em (N))
            perda = criterio(saida_logits, alvo_tokens.flatten())
            
            # Backpropagation
            otimizador.zero_grad()
            perda.backward()
            
            # Clipping e Otimização
            torch.nn.utils.clip_grad_norm_(modelo_treino.parameters(), max_norm=1.0)
            otimizador.step()
            
            perda_acumulada += perda.item()
            num_lotes += 1
            num_passos_total += 1

            if num_passos_total % 100 == 0:
                perda_media = perda_acumulada / num_lotes if num_lotes > 0 else 0
                print(f"Época {epoca} | Passo: {num_passos_total} | Perda Média: {perda_media:.4f}")

        # Relatório do Fim da Época
        perda_media_epoca = perda_acumulada / num_lotes if num_lotes > 0 else 0
        t_epoca = time.time() - t_inicio
        print(f"\n--- Época {epoca}/{NUM_EPOCAS} Concluída ---")
        print(f"Perda Média da Época: {perda_media_epoca:.4f}")
        
        # Salva os pesos
        torch.save(modelo_treino.state_dict(), CAMINHO_PESOS)
        print(f"Pesos salvos em: {CAMINHO_PESOS}")

    print("\n\n✅ TREINAMENTO AUTOMÁTICO CONCLUÍDO!")

# --- EXECUÇÃO PRINCIPAL ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lura AI: Servidor Chatbot ou Treinamento Automático.")
    
    parser.add_argument('--train', action='store_true', 
                        help='Executa o módulo de treinamento automático em vez de iniciar o servidor web.')
    
    args = parser.parse_args()

    if args.train:
        run_training()
    else:
        print("--- Lura AI Chat Server ---")
        print("Para treinar o modelo, execute: python lura.py --train")
        app.run(debug=True)