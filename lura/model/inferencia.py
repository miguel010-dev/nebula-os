import torch
import os
import sys

# Adiciona diretório pai ao path para importar 'utils' e 'model'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Importa as ferramentas necessárias
from utils.tokenizer import Tokenizer
from model.modelo import ModeloLeve

# --- CONFIGURAÇÃO ---
CAMINHO_PESOS = os.path.join(os.path.dirname(__file__), "modelo_treinado.pth")
VOCAB_PATH = "data/textos.txt"

# Variáveis globais para armazenar a instância do modelo e do tokenizer
# Serão inicializadas na primeira chamada de inferencia
tokenizer = None
modelo = None

# --- FUNÇÃO DE INICIALIZAÇÃO E CARREGAMENTO DO MODELO ---

def inicializar_modelo():
    """Inicializa o modelo e o tokenizer, carregando os pesos."""
    global tokenizer, modelo
    
    if modelo is not None and tokenizer is not None:
        return modelo, tokenizer

    print("⚙️ Inicializando Tokenizer e Modelo...")

    # 1. Inicializa o Tokenizer e constrói/carrega o vocabulário
    tokenizer = Tokenizer()
    try:
        with open(VOCAB_PATH, "r", encoding="utf-8") as f:
            textos_completos = f.read()
        tokenizer.build_vocab([textos_completos])
    except FileNotFoundError:
        print(f"⚠️ Aviso: Arquivo {VOCAB_PATH} não encontrado. O vocabulário será limitado.")

    # 2. Inicializa o Modelo
    modelo = ModeloLeve(vocab_size=tokenizer.vocab_size)
    device = modelo.device 

    # 3. Carrega os pesos se existirem
    if os.path.exists(CAMINHO_PESOS):
        try:
            modelo.load_state_dict(torch.load(CAMINHO_PESOS, map_location=device))
            print(f"✅ Pesos carregados de: {CAMINHO_PESOS}")
        except RuntimeError as e:
            print(f"❌ Erro ao carregar pesos (tamanho incompatível?): {e}")
    else:
        print("❌ Arquivo de pesos não encontrado. Modelo usará pesos aleatórios.")

    modelo.eval()
    print("✅ Módulo model/inferencia.py inicializado com sucesso.")
    return modelo, tokenizer


# --- FUNÇÃO PRINCIPAL DE INFERÊNCIA ---

def prever_resposta(chat_historico, nova_pergunta, seq_len=50, max_new_tokens=1):
    """
    Gera uma resposta do modelo e salva os tensores de contexto/alvo para feedback.
    """
    
    # ❗ CHAMADA DE INICIALIZAÇÃO LENTA (Lazy Initialization)
    # Garante que o modelo só seja carregado na primeira chamada da função.
    global modelo, tokenizer
    if modelo is None or tokenizer is None:
        modelo, tokenizer = inicializar_modelo()
    
    # Coloca o modelo em modo de avaliação (não-treino)
    modelo.eval()
    device = modelo.device 

    # 1. Prepara o contexto de entrada
    contexto_str = f"Usuário: {nova_pergunta}"
    for item in chat_historico[-5:]:
         contexto_str = f"Usuário: {item['pergunta']} | Lura: {item['resposta']} | " + contexto_str
         
    # 2. Tokenização e Truncagem
    contexto_tokens = tokenizer.texto_para_tokens(contexto_str)
    if len(contexto_tokens) > seq_len:
        contexto_tokens = contexto_tokens[-seq_len:]
    
    # 3. Converte para Tensor
    entrada_tensor = torch.tensor([contexto_tokens], dtype=torch.long).to(device)
    
    # 4. Geração de Resposta (Single-step prediction)
    
    with torch.no_grad():
        # Usa o método forward_single_step
        logits, _ = modelo.forward_single_step(entrada_tensor)
        
        # Pega a previsão do próximo token (alvo)
        probs = torch.softmax(logits, dim=-1)
        alvo_token_id = torch.argmax(probs, dim=-1).item()
        
        # O Tensor alvo é apenas o ID do token previsto
        alvo_tensor = torch.tensor([alvo_token_id], dtype=torch.long).to(device)

    # 5. Pós-processamento
    resposta_str = tokenizer.tokens_para_texto([alvo_token_id])

    # Retorna a resposta, o token alvo (previsto) e o contexto de entrada
    return resposta_str, alvo_tensor, entrada_tensor