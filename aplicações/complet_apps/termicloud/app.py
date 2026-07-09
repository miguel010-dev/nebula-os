from flask import Flask, redirect, request,url_for

app = Flask(__name__)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

def comandoprint(mensagem):
    valor = request.form.get()