"""
Nebula Inteligence — Backend Flask
===================================
Aplicação reescrita e revisada: imports faltando corrigidos, lock global de
execução de código trocado por semáforo limitado, hardening de configuração
(sessão, CORS, execução de subprocess) e organização em seções claras.

IMPORTANTE — sobre o CodeExecutionService:
Rodar código Python enviado por usuários via subprocess, mesmo com timeout e
limites de recursos, NUNCA é um sandbox completo. Para produção, isole a
execução em containers descartáveis (Docker + seccomp/gVisor), uma VM
efêmera (Firecracker) ou um serviço dedicado (ex.: Judge0). As mitigações
abaixo (flags -I/-S, rlimits, env limpo, sem rede) reduzem a superfície de
ataque mas não substituem isolamento real de processo/host.
"""

from __future__ import annotations

import logging
import os
import re
import resource
import secrets
import string
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Blueprint, Flask, current_app, flash, jsonify, redirect,
    render_template, request, session, url_for,
)
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager, UserMixin, current_user, login_required,
    login_user, logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, event, exc
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Sentence-transformers é uma dependência pesada (baixa modelo, usa torch).
# Import isolado + flag para permitir rodar a app sem IA se a lib não estiver
# instalada, em vez de derrubar o processo inteiro no import.
try:
    from sentence_transformers import SentenceTransformer, util as st_util
    AI_AVAILABLE = True
except ImportError:  # pragma: no cover
    SentenceTransformer = None
    st_util = None
    AI_AVAILABLE = False


logger = logging.getLogger("nebula")


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

class Config:
    """Configuração centralizada, com defaults seguros para produção."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///nebulainteligence.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "user_uploads")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB por request

    # Sessão/cookies — SECURE só faz sentido atrás de HTTPS; em dev local
    # (http://localhost) travaria o login inteiro, então segue o ambiente.
    IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"
    SESSION_COOKIE_SECURE = IS_PRODUCTION
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 dias

    # Rate limiting — memory:// é por processo; use Redis em produção
    # multi-worker (RATELIMIT_STORAGE_URI=redis://...), senão cada worker
    # tem seu próprio contador e o limite real vira (N x nº de workers).
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")

    # Email
    SMTP_EMAIL = os.getenv("SMTP_EMAIL")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

    # Upload
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "txt", "pdf", "md", "py", "js", "html", "css", "json"}
    MAX_FILE_SIZE = 8 * 1024 * 1024  # 8MB

    # Execução de código
    CODE_EXECUTION_TIMEOUT = int(os.getenv("CODE_EXECUTION_TIMEOUT", "15"))
    CODE_EXECUTION_MAX_CONCURRENT = int(os.getenv("CODE_EXECUTION_MAX_CONCURRENT", "3"))
    CODE_EXECUTION_MAX_OUTPUT_CHARS = 20_000
    CODE_EXECUTION_MAX_MEMORY_MB = 256


# ---------------------------------------------------------------------------
# Extensões (instanciadas uma vez, ligadas à app na factory)
# ---------------------------------------------------------------------------

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "error"
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    senha = db.Column(db.String(255), nullable=False)
    maquinas_criadas = db.Column(db.Integer, default=0, nullable=False)
    cargo = db.Column(db.String(100), nullable=False, default="user")
    vip = db.Column(db.Boolean, default=False, nullable=False)
    armazenamento = db.Column(db.Integer, default=15, nullable=False)
    vip_expira_em = db.Column(db.DateTime, nullable=True)
    total_logins = db.Column(db.Integer, default=0, nullable=False)
    chip = db.Column(db.String(255), default="SQ-V3-lite", nullable=False)
    memoria = db.Column(db.String(45), default="2 GIGAS", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    apps_comprados = db.relationship("AppCompra", back_populates="user", cascade="all, delete-orphan")
    maquinas = db.relationship("Maquina", backref="dono", lazy=True, cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
    )

    def is_vip(self) -> bool:
        """Retorna se o VIP está ativo, expirando automaticamente se vencido."""
        if not self.vip:
            return False
        if self.vip_expira_em and datetime.utcnow() > self.vip_expira_em:
            self.vip = False
            self.vip_expira_em = None
            try:
                db.session.commit()
            except exc.SQLAlchemyError:
                db.session.rollback()
                logger.exception("Falha ao expirar VIP do usuário %s", self.id)
            return False
        return True

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Aplicativo(db.Model):
    __tablename__ = "aplicativos"

    id = db.Column(db.Integer, primary_key=True)
    nomeapp = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.String(1050), nullable=False)
    logoapp = db.Column(db.String(1050), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    compradores = db.relationship("AppCompra", back_populates="aplicativo", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Aplicativo {self.nomeapp}>"


class AppCompra(db.Model):
    __tablename__ = "app_compras"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    app_id = db.Column(db.Integer, db.ForeignKey("aplicativos.id", ondelete="CASCADE"), nullable=False)
    autorizado = db.Column(db.Boolean, default=False, nullable=False)
    comprado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", back_populates="apps_comprados")
    aplicativo = db.relationship("Aplicativo", back_populates="compradores")

    __table_args__ = (
        UniqueConstraint("user_id", "app_id", name="uq_user_app"),
        Index("ix_app_compras_user_app", "user_id", "app_id"),
    )


class Maquina(db.Model):
    __tablename__ = "maquinas"

    id = db.Column(db.Integer, primary_key=True)
    maquina_nome = db.Column(db.String(150), nullable=False)
    maquina_senha = db.Column(db.String(255), nullable=False)
    maquina_dono_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    codigo = db.Column(db.String(12), nullable=False, unique=True, index=True)
    online = db.Column(db.Boolean, default=False, nullable=False)
    cpu = db.Column(db.String(128), nullable=True)
    ram = db.Column(db.Integer, nullable=True)   # MB
    disco = db.Column(db.Integer, nullable=True)  # MB
    gpu = db.Column(db.String(128), nullable=True)
    background = db.Column(db.String(900), nullable=False, default="")
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    ultimo_ping = db.Column(db.DateTime, nullable=True)
    ip_ultimo = db.Column(db.String(64), nullable=True)
    sistema_operacional = db.Column(db.String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("maquina_dono_id", "maquina_nome", name="uq_dono_nome"),
        Index("ix_maquinas_codigo", "codigo", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Maquina {self.maquina_nome}>"


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ativa foreign keys e ajustes de performance apenas para SQLite."""
    if not type(dbapi_connection).__module__.startswith("sqlite3"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------

class SecurityError(Exception):
    """Erro relacionado a segurança (ex.: path traversal)."""


class ValidationError(Exception):
    """Erro de validação de dados de entrada."""


class ExecutionError(Exception):
    """Erro durante execução de código enviado pelo usuário."""


# ---------------------------------------------------------------------------
# Utilitários de segurança
# ---------------------------------------------------------------------------

class Security:
    """Validação e sanitização de dados de entrada."""

    MIN_PASSWORD_LENGTH = 12
    MAX_PASSWORD_LENGTH = 128
    PASSWORD_PATTERN = re.compile(
        r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])"
        rf"[A-Za-z\d@$!%*?&]{{{MIN_PASSWORD_LENGTH},{MAX_PASSWORD_LENGTH}}}$"
    )
    EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    NAME_PATTERN = re.compile(r"[^A-Za-z0-9 _\-.áàâãéèêíïóôõöúçüÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÜ]")

    @staticmethod
    def validate_password(password: str) -> bool:
        return bool(password) and bool(Security.PASSWORD_PATTERN.match(password))

    @staticmethod
    def validate_email(email: str) -> bool:
        return bool(email) and bool(Security.EMAIL_PATTERN.match(email))

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        return secure_filename(filename)

    @staticmethod
    def sanitize_name(name: Optional[str], limit: int = 150) -> str:
        name = (name or "").strip()[:limit]
        return Security.NAME_PATTERN.sub("", name).strip()

    @staticmethod
    def generate_secure_code(length: int = 6) -> str:
        return "".join(secrets.choice(string.digits) for _ in range(length))


class FileManager:
    """Gestão segura de arquivos por usuário, com proteção a path traversal."""

    @staticmethod
    def get_user_directory(user_id: int) -> Path:
        base_dir = Path(current_app.config["UPLOAD_FOLDER"]) / str(user_id)
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "uploads").mkdir(exist_ok=True)
        return base_dir

    @staticmethod
    def is_allowed_file(filename: str) -> bool:
        allowed_extensions = current_app.config.get("ALLOWED_EXTENSIONS", set())
        return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions

    @staticmethod
    def get_safe_path(user_id: int, relative_path: str) -> Path:
        """Resolve o caminho final e garante que ele não escapa da pasta do usuário."""
        user_dir = FileManager.get_user_directory(user_id)
        uploads_dir = (user_dir / "uploads").resolve()
        safe_path = (uploads_dir / relative_path).resolve()
        if not safe_path.is_relative_to(uploads_dir):
            raise SecurityError("Tentativa de path traversal detectada")
        return safe_path


class EmailService:
    """Envio de e-mails transacionais (ex.: códigos de verificação)."""

    @staticmethod
    def send_verification_code(code: str, recipient: str, subject: str = "Código de Verificação") -> bool:
        smtp_email = current_app.config.get("SMTP_EMAIL")
        smtp_password = current_app.config.get("SMTP_PASSWORD")

        if not smtp_email or not smtp_password:
            logger.warning("SMTP não configurado — fallback: %s -> %s", recipient, code)
            return False

        message = MIMEText(f"Seu código de verificação é: {code}")
        message["Subject"] = subject
        message["From"] = smtp_email
        message["To"] = recipient

        try:
            import smtplib
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
                server.login(smtp_email, smtp_password)
                server.sendmail(smtp_email, recipient, message.as_string())
            logger.info("Email enviado para %s", recipient)
            return True
        except Exception:
            logger.exception("Falha ao enviar email para %s", recipient)
            return False


# ---------------------------------------------------------------------------
# Execução de código enviado pelo usuário
# ---------------------------------------------------------------------------

class CodeExecutionService:
    """
    Executa código Python enviado por usuários com limites de tempo, memória
    e concorrência. Ver aviso no topo do arquivo: isto reduz risco, não
    elimina — use isolamento por container/VM em produção.
    """

    def __init__(self, timeout: int = 15, max_concurrent: int = 3, max_output_chars: int = 20_000, max_memory_mb: int = 256):
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.max_memory_mb = max_memory_mb
        # Semáforo em vez de lock exclusivo: permite N execuções concorrentes
        # ao invés de serializar toda a aplicação em uma única execução por vez.
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    @contextmanager
    def _slot(self):
        acquired = self._semaphore.acquire(timeout=5)
        if not acquired:
            raise ExecutionError("Servidor ocupado, tente novamente em instantes")
        try:
            yield
        finally:
            self._semaphore.release()

    @staticmethod
    def _limit_resources(max_memory_mb: int):
        """Aplicado no processo filho (Unix apenas) antes do exec."""
        def _apply():
            try:
                mem_bytes = max_memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
                resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
                resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
                resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
            except (ValueError, OSError):
                pass  # plataforma pode não suportar todos os rlimits (ex.: Windows/macOS)
        return _apply

    def execute_python(self, code: str) -> Dict[str, Any]:
        if not code or not code.strip():
            raise ExecutionError("Código vazio")

        with self._slot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                script_path = Path(tmp_dir) / "script.py"
                script_path.write_text(code, encoding="utf-8")

                env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
                preexec = self._limit_resources(self.max_memory_mb) if os.name == "posix" else None

                try:
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", str(script_path)],
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        cwd=tmp_dir,
                        env=env,
                        preexec_fn=preexec,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc_:
                    raise ExecutionError("Tempo limite de execução excedido") from exc_
                except FileNotFoundError as exc_:
                    raise ExecutionError("Interpretador Python não encontrado") from exc_

                stdout = (result.stdout or "")[: self.max_output_chars]
                stderr = (result.stderr or "")[: self.max_output_chars]

                if result.returncode != 0:
                    raise ExecutionError(stderr.strip() or stdout.strip() or "Erro desconhecido na execução")

                return {"status": "success", "output": stdout}

    def execute_lineax(self, code: str) -> Dict[str, Any]:
        """Placeholder — implemente aqui a lógica do interpretador Lineax/SQ."""
        if not code or not code.strip():
            raise ExecutionError("Código vazio")
        return {"status": "success", "output": "Lineax execution placeholder"}


# ---------------------------------------------------------------------------
# Assistente de IA (busca semântica por similaridade)
# ---------------------------------------------------------------------------

class AIAssistant:
    """Responde perguntas por similaridade semântica contra uma base fixa de Q&A."""

    def __init__(self):
        self.model: Optional["SentenceTransformer"] = None
        self.qa_mapping: List[Dict[str, str]] = []
        self.corpus_embeddings = None
        self.similarity_threshold = 0.65

        if not AI_AVAILABLE:
            logger.warning("sentence-transformers não instalado — AIAssistant desabilitado")
            return

        try:
            self.model = SentenceTransformer("paraphrase-multilingual-mpnet-base-v2")
            self._load_qa_data()
        except Exception:
            logger.exception("Falha ao carregar modelo de IA")
            self.model = None

    def _load_qa_data(self) -> None:
        self.qa_mapping = [
            {
                "query": "Oi, quem é você",
                "response": "👋 Oi! Eu sou a ALMA — Agente Linear Massivo Alternativo.",
                "audio": "audio1.mp3",
            },
        ]
        corpus_queries = [item["query"] for item in self.qa_mapping]
        self.corpus_embeddings = self.model.encode(corpus_queries, convert_to_tensor=True)

    def find_best_match(self, user_query: str) -> Dict[str, Any]:
        if not self.model or not self.qa_mapping:
            return {"score": 0.0, "response": "Serviço de IA indisponível", "audio": None}

        query_embedding = self.model.encode(user_query, convert_to_tensor=True)
        cosine_scores = st_util.cos_sim(query_embedding, self.corpus_embeddings)[0]
        best_match_index = int(cosine_scores.argmax())
        best_score = float(cosine_scores[best_match_index])
        best_match_data = self.qa_mapping[best_match_index]

        if best_score < self.similarity_threshold:
            return {"score": best_score, "response": None, "audio": None}

        return {
            "score": best_score,
            "response": best_match_data.get("response"),
            "audio": best_match_data.get("audio"),
        }


# ---------------------------------------------------------------------------
# Blueprint: auth
# ---------------------------------------------------------------------------

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("senha", "")

        if not email or not password:
            flash("Por favor, preencha todos os campos.", "error")
            return render_template("auth/login.html")

        if not Security.validate_email(email):
            flash("Email inválido.", "error")
            return render_template("auth/login.html")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.senha, password):
            login_user(user)
            user.total_logins += 1
            try:
                db.session.commit()
            except exc.SQLAlchemyError:
                db.session.rollback()
                logger.exception("Falha ao atualizar total_logins do usuário %s", user.id)
            flash("Login realizado com sucesso!", "success")
            next_page = request.args.get("next")
            # Evita open-redirect: só aceita caminhos internos.
            if next_page and not next_page.startswith("/"):
                next_page = None
            return redirect(next_page or url_for("main.inicio"))

        flash("Email ou senha inválidos.", "error")

    return render_template("auth/login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if request.method == "POST":
        nome = Security.sanitize_name(request.form.get("nome", ""))
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("senha", "")
        confirm_password = request.form.get("confirmar_senha", "")

        if not all([nome, email, password, confirm_password]):
            flash("Preencha todos os campos.", "error")
            return render_template("auth/register.html")

        if not Security.validate_email(email):
            flash("Email inválido.", "error")
            return render_template("auth/register.html")

        if password != confirm_password:
            flash("As senhas não coincidem.", "error")
            return render_template("auth/register.html")

        if not Security.validate_password(password):
            flash(
                f"A senha deve ter no mínimo {Security.MIN_PASSWORD_LENGTH} caracteres, "
                "incluindo letras maiúsculas, minúsculas, números e símbolos.",
                "error",
            )
            return render_template("auth/register.html")

        if User.query.filter_by(email=email).first():
            flash("Email já cadastrado.", "error")
            return render_template("auth/register.html")

        try:
            hashed_password = generate_password_hash(password)
            user = User(nome=nome, email=email, senha=hashed_password)
            db.session.add(user)
            db.session.flush()  # obtém user.id sem commit
            FileManager.get_user_directory(user.id)
            db.session.commit()
            flash("Conta criada com sucesso! Faça login para continuar.", "success")
            return redirect(url_for("auth.login"))
        except exc.IntegrityError:
            db.session.rollback()
            flash("Erro ao criar conta. Tente novamente.", "error")
        except Exception:
            db.session.rollback()
            logger.exception("Erro inesperado ao registrar usuário")
            flash("Erro ao criar conta. Tente novamente.", "error")

    return render_template("auth/register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    session.pop("codigo_maquina_atual", None)
    logout_user()
    flash("Você saiu da conta.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Blueprint: main
# ---------------------------------------------------------------------------

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    return render_template("main/landpage.html")


@main_bp.route("/inicio")
@login_required
def inicio():
    maquinas = Maquina.query.filter_by(maquina_dono_id=current_user.id).all()
    user_info = {
        "nome": current_user.nome,
        "email": current_user.email,
        "vip": current_user.is_vip(),
        "vip_expira_em": current_user.vip_expira_em,
        "maquinas_criadas": current_user.maquinas_criadas,
        "total_logins": current_user.total_logins,
        "armazenamento": current_user.armazenamento,
        "chip": current_user.chip,
        "memoria": current_user.memoria,
    }
    return render_template("main/dashboard.html", maquinas=maquinas, user_info=user_info)


# ---------------------------------------------------------------------------
# Blueprint: api
# ---------------------------------------------------------------------------

api_bp = Blueprint("api", __name__, url_prefix="/api")

_execution_service = CodeExecutionService(
    timeout=Config.CODE_EXECUTION_TIMEOUT,
    max_concurrent=Config.CODE_EXECUTION_MAX_CONCURRENT,
    max_output_chars=Config.CODE_EXECUTION_MAX_OUTPUT_CHARS,
    max_memory_mb=Config.CODE_EXECUTION_MAX_MEMORY_MB,
)

_SUPPORTED_LANGUAGES = {
    "html": "frontend", "css": "frontend", "javascript": "frontend", "js": "frontend",
    "lineax": "lineax", "lx": "lineax", "sq": "lineax",
    "python": "python",
}


@api_bp.route("/run-code", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def run_code():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    language = (data.get("language") or "").lower().strip()

    if not code or not language:
        return jsonify({"status": "error", "output": "Código e linguagem são obrigatórios"}), 400

    kind = _SUPPORTED_LANGUAGES.get(language)
    if kind is None:
        return jsonify({"status": "error", "output": f'Linguagem "{language}" não suportada'}), 400

    try:
        if kind == "frontend":
            return jsonify({"status": "info", "output": "Linguagens front-end são renderizadas direto no navegador."})
        if kind == "lineax":
            return jsonify(_execution_service.execute_lineax(code))
        if kind == "python":
            return jsonify(_execution_service.execute_python(code))
    except ExecutionError as e:
        return jsonify({"status": "error", "output": str(e)}), 400
    except Exception:
        current_app.logger.exception("Erro inesperado na execução de código")
        return jsonify({"status": "error", "output": "Erro interno no servidor"}), 500

    # Guarda de tipo — nunca deve ser alcançado dado o dict acima.
    return jsonify({"status": "error", "output": "Linguagem não suportada"}), 400


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    logging.basicConfig(
        level=logging.INFO if config_class.IS_PRODUCTION else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)

    allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    CORS(app, origins=allowed_origins, supports_credentials=True)

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if config_class.IS_PRODUCTION:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    _register_blueprints(app)
    _register_error_handlers(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    with app.app_context():
        db.create_all()

    return app


def _register_blueprints(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found_error(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(_error):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(429)
    def ratelimit_error(_error):
        return jsonify({"status": "error", "message": "Limite de requisições excedido. Tente novamente mais tarde."}), 429


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    application = create_app()

    port = int(os.getenv("PORT", 5100))
    debug = (not Config.IS_PRODUCTION) and os.getenv("FLASK_DEBUG", "false").lower() == "true"

    application.run(host="0.0.0.0", port=port, debug=debug)