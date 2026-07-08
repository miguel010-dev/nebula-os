import torch
import sys
import os
import time
import torch.nn as nn
import torch.optim as optim  
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Importa o modelo e as ferramentas que você definiu
from model.modelo import ModeloLeve
from utils.tokenizer import Tokenizer

CAMINHO_TEXTOS = "data/textos.txt" # O caminho correto para o treino


# ...
NUM_EPOCAS = 10  # Aumentei para 10 épocas para um treino mais substancial
LR_TREINAMENTO = 0.001  # Taxa de aprendizado mais baixa para treino em massa
SEQUENCE_LENGTH = 50  # Tamanho da janela de contexto para treino (Janela maior)

# --- Inicialização ---
print("--- Inicializando o Treinamento Não-Supervisionado ---")
print(f"Lendo textos de: {CAMINHO_TEXTOS}")

# 1. Inicializa tokenizer e carrega vocabulário
tokenizer = Tokenizer()
try:
    with open(CAMINHO_TEXTOS, "r", encoding="utf-8") as f:
        textos_completos = f.read()
    # Construir vocabulário a partir do texto completo
    tokenizer.build_vocab([textos_completos])
except FileNotFoundError:
    print(f"ERRO: Arquivo {CAMINHO_TEXTOS} não encontrado. Certifique-se de que ele existe. Abortando.")
    sys.exit(1)

# 2. Inicializa o modelo
# Assume que ModeloLeve já está com hidden_size=256 e num_layers=2
modelo = ModeloLeve(vocab_size=tokenizer.vocab_size)
device = modelo.device  # Pega o dispositivo (CPU/CUDA)
modelo.train()  # Coloca o modelo em modo de treino

# 3. Preparação dos Dados (Tokens)
todos_tokens = tokenizer.texto_para_tokens(textos_completos)
# Converte a lista de tokens para um tensor Long
todos_tokens_tensor = torch.tensor(todos_tokens, dtype=torch.long)

# Otimizador e Função de Perda
otimizador = optim.Adam(modelo.parameters(), lr=LR_TREINAMENTO)
# Usamos ignore_index caso haja padding no futuro, mas por enquanto, nn.CrossEntropyLoss() é suficiente.
criterio = nn.CrossEntropyLoss() 

# --- Loop de Treinamento ---
t_inicio = time.time()
num_passos_total = 0

print(f"Vocabulário de tamanho: {tokenizer.vocab_size}")
print(f"Total de tokens para treino: {len(todos_tokens)}")

for epoca in range(1, NUM_EPOCAS + 1):
    h = None  # Reinicia o estado oculto a cada época
    perda_acumulada = 0
    num_lotes = 0

    # Itera sobre os dados com uma janela deslizante
    # O -1 garante que sempre haverá um token alvo
    for i in range(0, len(todos_tokens_tensor) - SEQUENCE_LENGTH - 1, SEQUENCE_LENGTH):
        
        # 1. Definir Entrada (Contexto) e Alvo (Próximo Token)
        # Entrada: Sequência de 50 tokens (tokens 0 a 49)
        entrada_tokens = todos_tokens_tensor[i : i + SEQUENCE_LENGTH].unsqueeze(0).to(device)
        # Alvo: Sequência de 50 tokens (tokens 1 a 50)
        alvo_tokens = todos_tokens_tensor[i + 1 : i + SEQUENCE_LENGTH + 1].to(device)
        
        # 2. Forward Pass
        saida_logits, h = modelo(entrada_tokens, h)
        
        # 3. Cálculo da Perda
        # (Batch_size * Seq_len, Vocab_size) vs (Batch_size * Seq_len)
        perda = criterio(saida_logits, alvo_tokens.flatten())
        
        # 4. Backpropagation
        otimizador.zero_grad()
        perda.backward()
        
        # 5. Clipping e Otimização
        torch.nn.utils.clip_grad_norm_(modelo.parameters(), max_norm=1.0)
        otimizador.step()
        
        perda_acumulada += perda.item()
        num_lotes += 1
        num_passos_total += 1

        # Detalhes de Progresso 
        if num_passos_total % 100 == 0: # Ajustei para 100 passos para feedback mais rápido
            perda_media = perda_acumulada / num_lotes if num_lotes > 0 else 0
            print(f"Época {epoca} | Passo: {num_passos_total} | Perda Média: {perda_media:.4f}")

    # Relatório do Fim da Época
    perda_media_epoca = perda_acumulada / num_lotes if num_lotes > 0 else 0
    t_epoca = time.time() - t_inicio
    print(f"\n--- Época {epoca}/{NUM_EPOCAS} Concluída ---")
    print(f"Perda Média da Época: {perda_media_epoca:.4f}")
    print(f"Tempo total decorrido: {t_epoca:.2f} segundos.")
    
    # Salva o modelo a cada época (para não perder o progresso)
    torch.save(modelo.state_dict(), CAMINHO_PESOS)
    print(f"Pesos salvos em: {CAMINHO_PESOS}")

print("\n\n✅ TREINAMENTO AUTOMÁTICO CONCLUÍDO!")