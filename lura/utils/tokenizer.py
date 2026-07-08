class Tokenizer:
    def __init__(self):
        self.palavra2idx = {"<unk>": 0}
        self.idx2palavra = {0: "<unk>"}

    def build_vocab(self, lista_textos):
        for texto in lista_textos:
            for palavra in texto.split():
                if palavra not in self.palavra2idx:
                    idx = len(self.palavra2idx)
                    self.palavra2idx[palavra] = idx
                    self.idx2palavra[idx] = palavra

    def texto_para_tokens(self, texto):
        tokens = []
        for palavra in texto.split():
            tokens.append(self.palavra2idx.get(palavra, 0))
        return tokens

    def token_para_palavra(self, token):
        return self.idx2palavra.get(token, "<unk>")

    @property
    def vocab_size(self):
        return len(self.palavra2idx)
