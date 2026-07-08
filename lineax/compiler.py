import os
import threading
import json
import re
import requests
import time
import math
import random
from urllib.parse import urlparse

# Lock para evitar que múltiplos pedidos de execução rodem ao mesmo tempo
execution_lock = threading.Lock()

class LinexInterpreter:
    def __init__(self):
        self.variaveis = {}
        self.funcoes = {}
        self.entrada_simulada = []
        self.entrada_index = 0
        self.output = []
        self.return_value = None # Para o retorno de função
        self.safe_builtins = {
            'len': len,
            'str': str,
            'int': int,
            'float': float,
            'bool': bool,
            'math': math,
            'random': random
        }

    def _get_valor(self, expressao):
        """Obtém o valor de uma expressão ou variável."""
        expressao = expressao.strip()
        
        # Se for uma string literal
        if expressao.startswith('"') and expressao.endswith('"'):
            return expressao.strip('"')

        # Se for um número
        try:
            if '.' in expressao:
                return float(expressao)
            return int(expressao)
        except ValueError:
            pass

        # Se for uma variável (acesso direto)
        if expressao in self.variaveis:
            return self.variaveis[expressao]
        
        # Se for acesso a propriedade de objeto JSON
        # Suporta acesso aninhado como 'variavel.prop1.prop2'
        match_prop = re.match(r"(\w+)\.(.+)", expressao)
        if match_prop:
            var_json, prop = match_prop.groups()
            if var_json in self.variaveis and isinstance(self.variaveis[var_json], dict):
                partes = prop.split('.')
                valor = self.variaveis[var_json]
                try:
                    for p in partes:
                        if isinstance(valor, dict):
                            valor = valor.get(p)
                        else:
                            valor = None
                            break
                    # Tratamento especial para booleans e None que podem ser retornados
                    if valor is not None or valor is False:
                        return valor
                except (TypeError, KeyError):
                    pass # Deixa cair para None se houver erro no acesso
        
        return None

    def _avaliar_expressao(self, expressao):
        """Avalia uma expressão com suporte a concatenação, variáveis e funções."""
        expressao = expressao.strip()
        
        # Tenta tratar concatenação de strings (prioridade, pois pode conter variáveis)
        if '+' in expressao and not re.search(r"[/*-]", expressao): # Evita conflito com expressões matemáticas
            partes = expressao.split('+')
            conteudo = ""
            for p in partes:
                valor = self._get_valor(p.strip())
                if valor is not None:
                    # Força a conversão para string para concatenação
                    conteudo += str(valor)
            if conteudo:
                return conteudo
        
        # Tenta avaliar uma expressão matemática complexa ou built-ins
        try:
            # Cria um namespace com as variáveis e built-ins seguros
            local_vars = {k: v for k, v in self.variaveis.items() if not isinstance(v, (dict, list))}
            local_vars.update(self.safe_builtins)
            
            # Substitui os nomes das variáveis na expressão antes de avaliar
            expr_formatada = expressao
            for var_nome, var_valor in self.variaveis.items():
                if isinstance(var_valor, (int, float, bool)):
                    expr_formatada = re.sub(r'\b' + re.escape(var_nome) + r'\b', str(var_valor), expr_formatada)
            
            return eval(expr_formatada, {"__builtins__": {}}, local_vars) # Avalia de forma restrita
        except (NameError, TypeError, SyntaxError):
            pass
            
        # Se não for uma expressão complexa, tenta avaliar como um valor simples (literal ou variável)
        valor = self._get_valor(expressao)
        if valor is not None:
            return valor

        # Tenta buscar uma variável que pode não ter sido capturada pelo _get_valor
        if expressao in self.variaveis:
             return self.variaveis[expressao]

        raise ValueError(f"Expressão inválida ou variável não definida: '{expressao}'")

    def _avaliar_condicao(self, expressao):
        """Avalia uma condição de forma segura."""
        expressao = expressao.replace("AND", " and ").replace("OR", " or ").replace("NOT", " not ")
        
        # Simples avaliação de igualdade/comparação (prioritária)
        match = re.match(r"(.+?)\s*(==|!=|>|<|>=|<=)\s*(.+)", expressao.strip())
        if match:
            left, op, right = match.groups()
            valor_left = self._avaliar_expressao(left.strip())
            valor_right = self._avaliar_expressao(right.strip())

            if op == "==": return valor_left == valor_right
            if op == "!=": return valor_left != valor_right
            if op == ">": return valor_left > valor_right
            if op == "<": return valor_left < valor_right
            if op == ">=": return valor_left >= valor_right
            if op == "<=": return valor_left <= valor_right
            return False

        # Avalia a expressão completa como booleana
        try:
            return bool(self._avaliar_expressao(expressao))
        except ValueError:
            return False # Se não puder avaliar, considera falso

    def _executar_comando(self, comando, linha_num):
        """Executa um único comando da linguagem Linex."""
        partes = comando.strip().split(maxsplit=2) # Aumentado para 2 para melhor parsing
        if not partes: return
        comando_principal = partes[0].upper()
        argumentos = partes[1] if len(partes) > 1 else ""
        
        # Comando Avançado de Impressão (SYS ECHO)
        if comando_principal == "SYS" and argumentos.upper().startswith("ECHO"):
            conteudo_expr = argumentos.split(maxsplit=1)[1] if len(argumentos.split(maxsplit=1)) > 1 else ""
            if not conteudo_expr:
                raise SyntaxError("Uso incorreto. Formato: SYS ECHO <expressao>")
            conteudo = self._avaliar_expressao(conteudo_expr)
            self.output.append(f"📢 {conteudo}")
        
        # Comando Avançado de Atribuição (VAR SET)
        elif comando_principal == "VAR" and argumentos.upper().startswith("SET"):
            match = re.match(r"SET\s+(\w+)\s*=\s*(.*)", argumentos, re.IGNORECASE)
            if not match: 
                raise SyntaxError("Uso incorreto. Formato: VAR SET nome = valor")
            nome_var, valor_expr = match.groups()
            
            if valor_expr.strip().upper().startswith("CALC"):
                expressao_calc = valor_expr.strip().split(maxsplit=1)[1]
                valor = self._avaliar_expressao(expressao_calc)
            else:
                valor = self._avaliar_expressao(valor_expr)
            
            self.variaveis[nome_var] = valor
            self.output.append(f"✅ Variável '{nome_var}' configurada para: {valor}")
            
        # Comando de Input (INPUT READ)
        elif comando_principal == "INPUT" and argumentos.upper().startswith("READ"):
            nome_var = argumentos.split(maxsplit=1)[1].strip() if len(argumentos.split(maxsplit=1)) > 1 else ""
            if not nome_var:
                raise SyntaxError("Uso incorreto. Formato: INPUT READ <nome_da_variavel>")
            
            if self.entrada_index < len(self.entrada_simulada):
                valor_input = self.entrada_simulada[self.entrada_index]
                self.entrada_index += 1
            else:
                valor_input = "Entrada do usuário (Simulado)"
                
            self.variaveis[nome_var] = valor_input
            self.output.append(f"⌨️ Variável '{nome_var}' recebeu entrada simulada: '{valor_input}'")

        # Comando de Cálculo Simples (CALC)
        elif comando_principal == "CALC":
            if not argumentos:
                raise SyntaxError("Uso incorreto. Formato: CALC <expressao>")
            resultado = self._avaliar_expressao(argumentos)
            self.output.append(f"🧮 Resultado: {resultado}")
            
        # Comando de HTTP Avançado (HTTP GET|POST)
        elif comando_principal == "HTTP":
            match_get = re.match(r"GET\s+\"(.*?)\"\s+TO\s+(\w+)\s+STATUS\s+(\w+)\s*HEADERS\s*(\w+)?", argumentos, re.IGNORECASE)
            match_post = re.match(r"POST\s+\"(.*?)\"\s+DATA\s+(\w+)\s+TO\s+(\w+)\s+STATUS\s+(\w+)\s*HEADERS\s*(\w+)?", argumentos, re.IGNORECASE)
            
            url, nome_var_resp, nome_var_status, nome_var_headers, data_payload = None, None, None, None, None
            
            if match_get:
                url, nome_var_resp, nome_var_status, nome_var_headers = match_get.groups()
                method = "GET"
            elif match_post:
                url, data_payload_var, nome_var_resp, nome_var_status, nome_var_headers = match_post.groups()
                data_payload = self.variaveis.get(data_payload_var)
                if data_payload is None:
                    raise NameError(f"Variável de dados '{data_payload_var}' para POST não definida.")
                method = "POST"
            else:
                raise SyntaxError("Uso incorreto. Formato: HTTP GET \"url\" TO <var_resp> STATUS <var_status> [HEADERS <var_headers>] | HTTP POST \"url\" DATA <var_dados> TO <var_resp> STATUS <var_status> [HEADERS <var_headers>]")
            
            nome_var_headers = nome_var_headers or f"{nome_var_resp}_HEADERS" # Nome padrão se não for fornecido
            
            # Headers básicos para o POST se o payload for JSON
            request_headers = {}
            if method == "POST" and isinstance(data_payload, dict):
                 request_headers = {"Content-Type": "application/json"}

            try:
                if method == "GET":
                    response = requests.get(url, timeout=10)
                elif method == "POST":
                    # Converte o payload para JSON string se for um dicionário
                    data_to_send = json.dumps(data_payload) if isinstance(data_payload, dict) else data_payload
                    response = requests.post(url, data=data_to_send, headers=request_headers, timeout=10)
                
                self.variaveis[nome_var_resp] = response.text
                self.variaveis[nome_var_status] = response.status_code
                
                # Salva os headers da resposta em uma variável JSON
                self.variaveis[nome_var_headers] = dict(response.headers)

                self.output.append(f"🌐 Requisição {method} para `{url}` (Status: {response.status_code}). Conteúdo salvo em `{nome_var_resp}`.")
            
            except requests.exceptions.RequestException as e:
                self.variaveis[nome_var_resp] = None
                self.variaveis[nome_var_status] = 0
                self.variaveis[nome_var_headers] = {}
                raise RuntimeError(f"Erro na requisição para `{url}`: {e}")

        # Comando de Chamada de Função (CALL PROC)
        elif comando_principal == "CALL" and argumentos.upper().startswith("PROC"):
            nome_funcao = argumentos.split(maxsplit=1)[1].strip() if len(argumentos.split(maxsplit=1)) > 1 else ""
            if not nome_funcao:
                raise SyntaxError("Uso incorreto. Formato: CALL PROC <nome_funcao>")
            
            if nome_funcao not in self.funcoes:
                raise NameError(f"Função '{nome_funcao}' não definida.")
            
            self.output.append(f"➡️ Chamando função '{nome_funcao}'...")
            self.return_value = None # Reset do valor de retorno
            self._executar_bloco(self.funcoes[nome_funcao], 0)
            
            # Se a função retornou um valor, ele está em self.return_value
            if self.return_value is not None:
                 self.output.append(f"⬅️ Função '{nome_funcao}' finalizada com retorno: {self.return_value}.")
            else:
                 self.output.append(f"⬅️ Função '{nome_funcao}' finalizada.")

        # Comando de Retorno de Função (FUNCTION RETURN)
        elif comando_principal == "FUNCTION" and argumentos.upper().startswith("RETURN"):
            retorno_expr = argumentos.split(maxsplit=1)[1].strip() if len(argumentos.split(maxsplit=1)) > 1 else "0" # Retorno padrão 0
            valor_retorno = self._avaliar_expressao(retorno_expr)
            self.return_value = valor_retorno
            self.output.append(f"↩️ Função retornando valor: {valor_retorno}.")
            # O comando de retorno precisa levantar uma exceção para quebrar a execução do bloco
            raise StopIteration("Retorno de Função")
            
        # Comandos existentes (JSON, SAVE, LOAD)
        elif comando_principal == "JSON":
            match = re.match(r"LOAD\s+(\w+)\s+TO\s+(\w+)", argumentos, re.IGNORECASE)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: JSON LOAD <variavel_string> TO <variavel_json>")
            
            var_origem, var_destino = match.groups()
            if var_origem not in self.variaveis:
                raise NameError(f"Variável de origem '{var_origem}' não definida.")
            
            try:
                json_data = json.loads(self.variaveis[var_origem])
                self.variaveis[var_destino] = json_data
                self.output.append(f"📄 Conteúdo da variável '{var_origem}' carregado em formato JSON para '{var_destino}'.")
            except json.JSONDecodeError:
                raise ValueError(f"Conteúdo da variável '{var_origem}' não é um JSON válido.")
                
        elif comando_principal == "SAVE":
            match = re.match(r'"(.*)"', argumentos)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: SAVE \"nome_do_arquivo\"")
            filename = match.groups()[0]
            try:
                 with open(f"{filename}.json", "w") as f:
                    # Tenta serializar as variáveis; ignora objetos não serializáveis como bytes ou módulos
                    serializable_vars = {k: v for k, v in self.variaveis.items() if isinstance(v, (str, int, float, bool, dict, list, type(None)))}
                    json.dump(serializable_vars, f, indent=4)
                 self.output.append(f"💾 Variáveis salvas em {filename}.json")
            except Exception as e:
                raise RuntimeError(f"Erro ao salvar o arquivo: {e}")

        elif comando_principal == "LOAD":
            match = re.match(r'"(.*)"', argumentos)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: LOAD \"nome_do_arquivo\"")
            filename = match.groups()[0]
            if not os.path.exists(f"{filename}.json"):
                raise FileNotFoundError(f"Arquivo '{filename}.json' não encontrado.")
            with open(f"{filename}.json", "r") as f:
                data = json.load(f)
                self.variaveis.update(data)
            self.output.append(f"📂 Variáveis carregadas de {filename}.json")

        else:
            # Comandos de bloco não devem ser executados aqui, mas pode ocorrer se o parsing falhar
            raise SyntaxError(f"Comando desconhecido: '{comando_principal}'")

    def _executar_bloco(self, bloco, linha_inicial):
        """Executa um bloco de código (código principal, função, if, loop)."""
        i = 0
        while i < len(bloco):
            linha = bloco[i].strip()
            linha_num_real = linha_inicial + i
            if not linha or linha.startswith("#"):
                i += 1
                continue
            
            partes = linha.split(maxsplit=1)
            comando_principal = partes[0].upper()
            argumentos = partes[1] if len(partes) > 1 else ""

            # Função (FUNCTION)
            if comando_principal == "FUNCTION" and argumentos.upper().endswith("BEGIN"):
                match = re.match(r"(\w+)\s+BEGIN", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: FUNCTION <nome_funcao> BEGIN (linha {linha_num_real})")
                nome_funcao = match.groups()[0]
                
                bloco_func = []; j = i + 1
                while j < len(bloco) and not bloco[j].strip().upper().startswith("END FUNCTION"):
                    bloco_func.append(bloco[j])
                    j += 1
                if j >= len(bloco): raise SyntaxError(f"Bloco da função '{nome_funcao}' não fechado com 'END FUNCTION' (linha {linha_num_real})")
                
                self.funcoes[nome_funcao] = bloco_func
                self.output.append(f"📦 Função '{nome_funcao}' definida.")
                i = j + 1
            
            # Condicional (IF)
            elif comando_principal == "IF" and argumentos.upper().endswith("BEGIN"):
                match = re.match(r"(.*)\s+BEGIN", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: IF <condicao> BEGIN (linha {linha_num_real})")
                condicao_expr = match.groups()[0]
                
                try:
                    condicao_eh_verdadeira = self._avaliar_condicao(condicao_expr)
                except Exception as e:
                    raise type(e)(f"Erro na condição do 'IF': {e} (linha {linha_num_real})")
                
                bloco_if = []; j = i + 1
                bloco_else = []
                while j < len(bloco) and not bloco[j].strip().upper().startswith("ELSE") and not bloco[j].strip().upper().startswith("END IF"):
                    bloco_if.append(bloco[j])
                    j += 1
                
                if j < len(bloco) and bloco[j].strip().upper().startswith("ELSE"):
                    k = j + 1
                    while k < len(bloco) and not bloco[k].strip().upper().startswith("END IF"):
                        bloco_else.append(bloco[k])
                        k += 1
                    j = k
                        
                if j >= len(bloco) or not bloco[j].strip().upper().startswith("END IF"): 
                    raise SyntaxError(f"Bloco 'IF' não fechado com 'END IF' (linha {linha_num_real})")
                
                bloco_a_executar = bloco_if if condicao_eh_verdadeira else bloco_else
                log_msg = "✅ Condição verdadeira. Executando bloco 'IF'..." if condicao_eh_verdadeira else "❌ Condição falsa. Executando bloco 'ELSE'..."
                
                self.output.append(log_msg)
                try:
                    self._executar_bloco(bloco_a_executar, linha_num_real)
                except StopIteration:
                    # Permite o "FUNCTION RETURN" quebrar a execução de um bloco IF/ELSE
                    i = j + 1
                    raise # Propaga o StopIteration para fora

                i = j + 1
            
            # Loop (LOOP)
            elif comando_principal == "LOOP" and argumentos.upper().endswith("BEGIN"):
                match = re.match(r"(\d+)\s+BEGIN", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: LOOP <numero_vezes> BEGIN (linha {linha_num_real})")
                
                try:
                    vezes = int(match.groups()[0])
                except ValueError:
                    raise ValueError(f"O número de repetições deve ser um número inteiro (linha {linha_num_real})")
                
                bloco_loop = []; j = i + 1
                while j < len(bloco) and not bloco[j].strip().upper().startswith("END LOOP"):
                    bloco_loop.append(bloco[j])
                    j += 1
                if j >= len(bloco): raise SyntaxError(f"Bloco 'LOOP' não fechado com 'END LOOP' (linha {linha_num_real})")
                
                self.output.append(f"🔄 Iniciando LOOP por {vezes} vezes...")
                for _ in range(vezes):
                    try:
                        self._executar_bloco(bloco_loop, linha_num_real)
                    except StopIteration:
                        # Permite o "FUNCTION RETURN" quebrar o loop
                        break
                self.output.append("✅ LOOP finalizado.")
                i = j + 1
                
            # Fim de bloco (para evitar erro de comando desconhecido)
            elif comando_principal in ["END", "ELSE", "FUNCTION", "IF", "LOOP"] :
                i += 1 # Apenas pula estas linhas de fechamento
                continue

            # Comandos comuns
            else:
                try:
                    self._executar_comando(linha, linha_num_real)
                except StopIteration:
                     # Captura o StopIteration do FUNCTION RETURN e propaga para fora do bloco
                     return # Sai do bloco imediatamente
                except (SyntaxError, ValueError, NameError, FileNotFoundError, RuntimeError) as e:
                    raise type(e)(f"{e} (linha {linha_num_real})")
                except Exception as e:
                    raise Exception(f"Erro inesperado: {e} (linha {linha_num_real})")
                i += 1
        
    def executar_codigo_lineax(self, codigo, input_data=None):
        self.variaveis = {}; self.funcoes = {}; self.output = []
        if input_data:
            self.entrada_simulada = list(input_data)
        self.entrada_index = 0
        self.return_value = None
        
        # Limpa o código, mantendo apenas linhas não vazias e não-comentário
        linhas = [linha for linha in codigo.splitlines() if linha.strip() and not linha.strip().startswith("#")]
        
        if not linhas or not linhas[0].strip().upper().startswith("PROJECT START"):
            return ["❌ Erro: O projeto deve começar com 'PROJECT START'."]
        
        try:
            self.output.append("✅ Projeto Linex Avançado Iniciado (PROJECT START)!")
            self._executar_bloco(linhas[1:], 1)
            self.output.append("\n**--- Fim da Execução ---**")
            return self.output
        except Exception as e:
            return [f"❌ Erro na execução: {str(e)}"]

def executar_codigo_lineax(codigo, input_data=None):
    """Função de wrapper para executar o código Linex com um lock de thread."""
    with execution_lock:
        interpretador = LinexInterpreter()
        return interpretador.executar_codigo_lineax(codigo, input_data)