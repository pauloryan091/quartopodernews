from __future__ import annotations
import uuid
import imghdr
from werkzeug.utils import secure_filename
import os
import shutil
import sqlite3
import json
import hashlib
import secrets
import smtplib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import ssl
import re
import unicodedata

from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, Response
from flask_cors import CORS
import base64

# =====================================================
# CONFIGURAÇÃO DE EMAIL
# =====================================================
EMAIL_CONFIG = {
    'sender_email': os.getenv('GMAIL_USER', 'quartopodernews.sup1@gmail.com'),
    'sender_password': os.getenv('GMAIL_PASS', 'rflb xgvq bicp ygge'),
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'company_name': 'Quarto Poder News',
    'newsletter_name': 'Quarto Poder News Daily'
}

# =====================================================
# CONFIGURAÇÃO DO APP
# =====================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "quartopodernews.db"
STATIC_DIR = BASE_DIR
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "quartopoder-news-2026-secure-key-change-in-production")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

CORS(app, supports_credentials=True, origins=["http://127.0.0.1:5000", "http://localhost:5000"])
# =====================================================
# CONFIGURAÇÃO DE UPLOAD DE IMAGENS
# =====================================================
UPLOAD_FOLDER = BASE_DIR / 'static' / 'uploads'
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
# =====================================================
# BANCO DE DADOS SQLite - OTIMIZADO
# =====================================================
class Database:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_db()
        return cls._instance
    
    def _init_db(self):
        """Inicializa conexão única com otimizações"""
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        
        # PRAGMAS para performance
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA cache_size = -10000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        
        self._create_tables()
        self._update_schema()  # ADICIONADO: Atualizar schema existente
        self._seed_data()
        self._create_indexes()  # Criar índices após garantir que tudo existe
    
    def _create_tables(self):
        """Cria tabelas otimizadas - SEM CONSTRAINT UNIQUE NO SLUG INICIALMENTE"""
        cursor = self.conn.cursor()
        
        # Usuários
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL CHECK(perfil IN ('admin', 'jornalista')),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'inactive')),
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            observacoes TEXT,
            ultimo_login TIMESTAMP
        )
        ''')
        
        # Notícias - COM CAMPO SLUG (SEM UNIQUE INICIALMENTE)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS noticias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            subtitulo TEXT,
            conteudo TEXT NOT NULL,
            categoria TEXT NOT NULL,
            autor TEXT NOT NULL,
            autor_id INTEGER,
            imagem_url TEXT,
            data_publicacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data_atualizacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            visualizacoes INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'publicada' CHECK(status IN ('rascunho', 'publicada', 'arquivada')),
            tags TEXT,
            destaque BOOLEAN DEFAULT 0,
            fonte TEXT,
            slug TEXT,  -- CAMPO SLUG ADICIONADO (SEM UNIQUE AINDA)
            enviada_newsletter BOOLEAN DEFAULT 0
        )
        ''')
        
        # Categorias
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            descricao TEXT,
            cor TEXT DEFAULT '#003366',
            icon TEXT DEFAULT 'fas fa-folder',
            ordem INTEGER DEFAULT 0,
            visivel BOOLEAN DEFAULT 1
        )
        ''')
        
        # Inscritos na Newsletter
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS inscritos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            nome TEXT,
            codigo_confirmacao TEXT NOT NULL,
            confirmado BOOLEAN DEFAULT 0,
            receber_destaques BOOLEAN DEFAULT 1,
            receber_todas BOOLEAN DEFAULT 0,
            categorias_preferidas TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ultimo_envio TIMESTAMP,
            total_envios INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ativo' CHECK(status IN ('ativo', 'inativo', 'cancelado'))
        )
        ''')
        
        # Envios de Newsletter
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS envios_newsletter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            conteudo TEXT NOT NULL,
            noticias_ids TEXT,
            destinatarios_total INTEGER DEFAULT 0,
            destinatarios_entregues INTEGER DEFAULT 0,
            aberturas INTEGER DEFAULT 0,
            cliques INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'rascunho' CHECK(status IN ('rascunho', 'enviando', 'enviado', 'cancelado')),
            criado_por INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            enviado_em TIMESTAMP,
            erro TEXT
        )
        ''')
        
        self.conn.commit()
    
    def _update_schema(self):
        """Atualiza schema existente para adicionar coluna slug se necessário"""
        cursor = self.conn.cursor()
        
        try:
            # Verificar se a coluna slug já existe
            cursor.execute("PRAGMA table_info(noticias)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'slug' not in column_names:
                print("🔄 Adicionando coluna 'slug' à tabela noticias...")
                # ADICIONAR SEM CONSTRAINT UNIQUE INICIALMENTE
                cursor.execute('ALTER TABLE noticias ADD COLUMN slug TEXT')
                self.conn.commit()
                print("✅ Coluna 'slug' adicionada (sem constraint unique)")
                
                # Gerar slugs para notícias existentes
                cursor.execute('SELECT id, titulo FROM noticias WHERE slug IS NULL OR slug = ""')
                noticias_sem_slug = cursor.fetchall()
                
                print(f"📝 Gerando slugs para {len(noticias_sem_slug)} notícias existentes...")
                
                slugs_gerados = []
                for noticia in noticias_sem_slug:
                    noticia_id = noticia['id']
                    titulo = noticia['titulo']
                    
                    # Gerar slug base
                    slug_base = self._gerar_slug(titulo)
                    
                    # Verificar se já existe e tornar único
                    slug_final = slug_base
                    counter = 1
                    while slug_final in slugs_gerados:
                        slug_final = f"{slug_base}-{counter}"
                        counter += 1
                    
                    slugs_gerados.append(slug_final)
                    
                    cursor.execute('UPDATE noticias SET slug = ? WHERE id = ?', (slug_final, noticia_id))
                
                self.conn.commit()
                print(f"✅ Slugs gerados para {len(noticias_sem_slug)} notícias existentes")
                
                # AGORA adicionar constraint UNIQUE via nova tabela
                print("🔄 Adicionando constraint UNIQUE ao slug...")
                self._add_unique_constraint()
                
        except Exception as e:
            print(f"⚠️  Erro ao atualizar schema: {e}")
            self.conn.rollback()
    
    def _add_unique_constraint(self):
        """Adiciona constraint UNIQUE ao slug via recriação da tabela"""
        try:
            cursor = self.conn.cursor()
            
            # 1. Criar tabela temporária com os dados
            cursor.execute('''
            CREATE TABLE noticias_temp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                subtitulo TEXT,
                conteudo TEXT NOT NULL,
                categoria TEXT NOT NULL,
                autor TEXT NOT NULL,
                autor_id INTEGER,
                imagem_url TEXT,
                data_publicacao TIMESTAMP,
                data_atualizacao TIMESTAMP,
                visualizacoes INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'publicada',
                tags TEXT,
                destaque BOOLEAN DEFAULT 0,
                fonte TEXT,
                slug TEXT UNIQUE,
                enviada_newsletter BOOLEAN DEFAULT 0
            )
            ''')
            
            # 2. Copiar dados da tabela antiga para a nova
            cursor.execute('''
            INSERT INTO noticias_temp 
            SELECT * FROM noticias
            ''')
            
            # 3. Remover tabela antiga
            cursor.execute('DROP TABLE noticias')
            
            # 4. Renomear tabela temporária para o nome original
            cursor.execute('ALTER TABLE noticias_temp RENAME TO noticias')
            
            self.conn.commit()
            print("✅ Constraint UNIQUE adicionada ao slug com sucesso!")
            
        except Exception as e:
            print(f"❌ Erro ao adicionar constraint UNIQUE: {e}")
            self.conn.rollback()
            raise
    
    def _create_indexes(self):
        """Cria índices para melhor performance"""
        cursor = self.conn.cursor()
        
        try:
            print("📊 Criando/atualizando índices...")
            
            # Índices para usuários
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios(email)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_usuarios_perfil ON usuarios(perfil, status)')
            
            # Índices para notícias (verificar se coluna existe primeiro)
            cursor.execute("PRAGMA table_info(noticias)")
            columns = cursor.fetchall()
            column_names = [col[1] for col in columns]
            
            # Só criar índice para slug se a coluna existir
            if 'slug' in column_names:
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_noticias_slug ON noticias(slug)')
                print("  ✓ Índice para slug criado")
            else:
                print("  ⚠️  Coluna slug não encontrada - pulando índice")
            
            # Outros índices para notícias
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_noticias_categoria ON noticias(categoria)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_noticias_status ON noticias(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_noticias_destaque ON noticias(destaque) WHERE destaque = 1')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_noticias_data ON noticias(data_publicacao DESC)')
            
            # Índices para outras tabelas
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_categorias_ordem ON categorias(ordem, visivel)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_inscritos_email ON inscritos(email)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_inscritos_status ON inscritos(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_inscritos_confirmado ON inscritos(confirmado) WHERE confirmado = 1')
            
            self.conn.commit()
            print("✅ Índices criados/atualizados com sucesso!")
            
        except Exception as e:
            print(f"⚠️  Erro ao criar índices: {e}")
            # Não fazemos rollback aqui - índices são opcionais
    
    def _seed_data(self):
        """Insere apenas os dados essenciais - SEM NOTÍCIAS ESTÁTICAS"""
        cursor = self.conn.cursor()
        
        # Verificar se já tem admin
        cursor.execute('SELECT COUNT(*) as c FROM usuarios WHERE email = ?', 
                      ('admin@quartopodernews.com',))
        if cursor.fetchone()['c'] == 0:
            senha_hash = self._hash_password('admin123')
            cursor.execute('''
            INSERT INTO usuarios (nome, email, senha_hash, perfil, status, observacoes)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', ('Administrador', 'admin@quartopodernews.com', senha_hash, 
                  'admin', 'active', 'Usuário administrador principal'))
            print("👤 Usuário admin criado")
        
        # Categorias padrão - APENAS ISSO É NECESSÁRIO
        categorias = [
            ('Política', 'Notícias sobre política', '#D50000', 'fas fa-landmark', 1),
            ('Economia', 'Notícias econômicas', '#27ae60', 'fas fa-chart-line', 2),
            ('Esportes', 'Notícias esportivas', '#f39c12', 'fas fa-futbol', 3),
            ('Cultura', 'Cultura e entretenimento', '#9b59b6', 'fas fa-theater-masks', 4),
            ('Tecnologia', 'Tecnologia e inovação', '#3498db', 'fas fa-microchip', 5),
            ('Saúde', 'Saúde e bem-estar', '#e74c3c', 'fas fa-heartbeat', 6),
        ]
        
        categorias_criadas = 0
        for cat in categorias:
            cursor.execute('''
            INSERT OR IGNORE INTO categorias (nome, descricao, cor, icon, ordem)
            VALUES (?, ?, ?, ?, ?)
            ''', cat)
            if cursor.rowcount > 0:
                categorias_criadas += 1
        
        self.conn.commit()
        if categorias_criadas > 0:
            print(f"📂 {categorias_criadas} categorias criadas")
    
    def _hash_password(self, password: str) -> str:
        """Gera hash seguro da senha"""
        salt = secrets.token_hex(16)
        return f"{salt}${hashlib.sha256((salt + password).encode()).hexdigest()}"
    
    def verify_password(self, stored_hash: str, password: str) -> bool:
        """Verifica senha"""
        if not stored_hash or '$' not in stored_hash:
            return False
        salt, hash_val = stored_hash.split('$', 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == hash_val
    
    # ========== MÉTODOS UTILITÁRIOS ==========
    
    def _gerar_slug(self, texto: str) -> str:
        """Gera slug amigável a partir do texto"""
        if not texto:
            return 'noticia'
        
        # Normalizar texto
        texto = unicodedata.normalize('NFKD', texto)
        texto = texto.encode('ASCII', 'ignore').decode('ASCII')
        
        # Converter para minúsculas e remover caracteres especiais
        slug = texto.lower()
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)  # Remove caracteres especiais
        slug = re.sub(r'\s+', '-', slug)  # Substitui espaços por hífens
        slug = re.sub(r'-+', '-', slug)  # Remove múltiplos hífens
        slug = slug.strip('-')  # Remove hífens das extremidades
        
        # Limitar tamanho do slug
        if len(slug) > 100:
            slug = slug[:100]
            slug = slug.rstrip('-')
        
        return slug if slug else 'noticia'
    
    def _gerar_slug_unico(self, titulo: str, slug_custom: str = None) -> str:
        """Gera um slug único para a notícia"""
        # Usar slug customizado ou gerar do título
        base_slug = slug_custom.strip() if slug_custom and slug_custom.strip() else self._gerar_slug(titulo)
        
        if not base_slug:
            base_slug = 'noticia'
        
        # Verificar se o slug já existe
        cursor = self.conn.cursor()
        slug = base_slug
        counter = 1
        
        while True:
            cursor.execute('SELECT id FROM noticias WHERE slug = ?', (slug,))
            if not cursor.fetchone():
                break
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        return slug
    
    # ========== MÉTODOS DE USUÁRIOS ==========
    
    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Busca usuário por email"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM usuarios WHERE email = ? AND status = "active"', 
                      (email.lower(),))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Busca usuário por ID"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM usuarios WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def authenticate_user(self, email: str, password: str) -> Optional[Dict]:
        """Autentica usuário"""
        user = self.get_user_by_email(email)
        if user and self.verify_password(user['senha_hash'], password):
            # Atualiza último login
            cursor = self.conn.cursor()
            cursor.execute('UPDATE usuarios SET ultimo_login = CURRENT_TIMESTAMP WHERE id = ?',
                          (user['id'],))
            self.conn.commit()
            return user
        return None
    

    def list_usuarios(self) -> List[Dict]:
        
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, nome, email, perfil, status, criado_em, atualizado_em, observacoes, ultimo_login FROM usuarios ORDER BY criado_em DESC')
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def create_usuario(self, data: Dict) -> Optional[Dict]:
     
        try:
            nome = (data.get('nome') or '').strip()
            email = (data.get('email') or '').strip().lower()
            senha = data.get('senha') or ''
            perfil = (data.get('perfil') or '').strip()
            observacoes = (data.get('observacoes') or '').strip()
            status = (data.get('status') or 'active').strip()

            if not nome or not email or not senha or not perfil:
                return None
            if perfil not in ('admin', 'jornalista'):
                return None
            if status not in ('active', 'inactive'):
                status = 'active'

            senha_hash = self._hash_password(senha)

            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO usuarios (nome, email, senha_hash, perfil, status, observacoes)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (nome, email, senha_hash, perfil, status, observacoes))
            user_id = cursor.lastrowid
            self.conn.commit()
            return self.get_user_by_id(user_id)
        except Exception as e:
            print(f"Erro ao criar usuário: {e}")
            self.conn.rollback()
            return None

    def update_usuario(self, user_id: int, data: Dict) -> Optional[Dict]:
       
        try:
            cursor = self.conn.cursor()
            updates = ['atualizado_em = CURRENT_TIMESTAMP']
            params = []

            if 'nome' in data:
                updates.append('nome = ?')
                params.append((data.get('nome') or '').strip())

            if 'email' in data:
                updates.append('email = ?')
                params.append((data.get('email') or '').strip().lower())

            if 'perfil' in data:
                perfil = (data.get('perfil') or '').strip()
                if perfil not in ('admin', 'jornalista'):
                    return None
                updates.append('perfil = ?')
                params.append(perfil)

            if 'observacoes' in data:
                updates.append('observacoes = ?')
                params.append((data.get('observacoes') or '').strip())

            if 'status' in data:
                status = (data.get('status') or '').strip()
                if status not in ('active', 'inactive'):
                    return None
                updates.append('status = ?')
                params.append(status)

            if 'senha' in data and data.get('senha'):
                senha = data.get('senha')
                if len(senha) < 6:
                    return None
                updates.append('senha_hash = ?')
                params.append(self._hash_password(senha))

            if len(updates) == 1:  # apenas atualizado_em
                return self.get_user_by_id(user_id)

            params.append(user_id)
            cursor.execute(f'UPDATE usuarios SET {", ".join(updates)} WHERE id = ?', params)
            self.conn.commit()
            return self.get_user_by_id(user_id)
        except Exception as e:
            print(f"Erro ao atualizar usuário: {e}")
            self.conn.rollback()
            return None

    def delete_usuario(self, user_id: int) -> bool:
       
        try:
            cursor = self.conn.cursor()
            cursor.execute('DELETE FROM usuarios WHERE id = ?', (user_id,))
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            print(f"Erro ao excluir usuário: {e}")
            self.conn.rollback()
            return False

    def toggle_usuario_status(self, user_id: int) -> Optional[Dict]:
       
        try:
            user = self.get_user_by_id(user_id)
            if not user:
                return None
            novo = 'inactive' if user.get('status') == 'active' else 'active'
            cursor = self.conn.cursor()
            cursor.execute('UPDATE usuarios SET status = ?, atualizado_em = CURRENT_TIMESTAMP WHERE id = ?', (novo, user_id))
            self.conn.commit()
            return self.get_user_by_id(user_id)
        except Exception as e:
            print(f"Erro ao alternar status do usuário: {e}")
            self.conn.rollback()
            return None

    # ========== MÉTODOS DE NOTÍCIAS - COM SLUG ==========
    
    def create_noticia(self, data: Dict) -> Optional[Dict]:
        """Cria nova notícia com slug"""
        try:
            cursor = self.conn.cursor()
            
            # Obter slug dos dados ou gerar do título
            titulo = data.get('titulo', 'noticia-sem-titulo')
            slug_custom = data.get('slug', '').strip()
            
            # Gerar slug único
            slug = self._gerar_slug_unico(titulo, slug_custom)
            
            # Garantir que destaque seja booleano
            destaque = data.get('destaque', False)
            if isinstance(destaque, str):
                destaque = destaque.lower() in ['true', '1', 'yes', 'sim']
            
            # Preparar dados para inserção
            insert_data = {
                'titulo': data.get('titulo', '').strip(),
                'subtitulo': data.get('subtitulo', data.get('chamada', '')).strip(),
                'conteudo': data.get('conteudo', '').strip(),
                'categoria': data.get('categoria', '').strip(),
                'autor': data.get('autor', 'Redação QPN').strip(),
                'autor_id': data.get('autor_id'),
                'imagem_url': data.get('imagem_url', '').strip(),
                'status': data.get('status', 'publicada'),
                'tags': data.get('tags', '').strip(),
                'destaque': destaque,
                'fonte': data.get('fonte', 'Quarto Poder News').strip(),
                'slug': slug
            }
            
            # Inserir notícia
            cursor.execute('''
            INSERT INTO noticias (
                titulo, subtitulo, conteudo, categoria, autor, autor_id,
                imagem_url, status, tags, destaque, fonte, slug
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                insert_data['titulo'],
                insert_data['subtitulo'],
                insert_data['conteudo'],
                insert_data['categoria'],
                insert_data['autor'],
                insert_data['autor_id'],
                insert_data['imagem_url'],
                insert_data['status'],
                insert_data['tags'],
                insert_data['destaque'],
                insert_data['fonte'],
                insert_data['slug']
            ))
            
            noticia_id = cursor.lastrowid
            self.conn.commit()
            return self.get_noticia_by_id(noticia_id)
            
        except sqlite3.IntegrityError as e:
            if 'UNIQUE constraint failed: noticias.slug' in str(e):
                # Slug duplicado, tentar novamente com número
                print(f"⚠️  Slug duplicado '{slug}', tentando novamente...")
                return self.create_noticia(data)  # Recursão para gerar novo slug
            raise
        except Exception as e:
            print(f"Erro ao criar notícia: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_noticia_by_id(self, noticia_id: int) -> Optional[Dict]:
        """Busca notícia por ID"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM noticias WHERE id = ?', (noticia_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def get_noticia_by_slug(self, slug: str) -> Optional[Dict]:
        """Busca notícia por slug"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM noticias WHERE slug = ?', (slug,))
        row = cursor.fetchone()
        if row:
            # Incrementar visualizações
            cursor.execute('UPDATE noticias SET visualizacoes = visualizacoes + 1 WHERE id = ?',
                          (row['id'],))
            self.conn.commit()
            return dict(row)
        return None
    
    def get_all_noticias(self, limit: int = 50, offset: int = 0, 
                        categoria: str = None, status: str = None) -> List[Dict]:
        """Lista notícias com filtros"""
        cursor = self.conn.cursor()
        query = 'SELECT * FROM noticias'
        params = []
        conditions = []
        
        if categoria:
            conditions.append('categoria = ?')
            params.append(categoria)
        
        if status:
            conditions.append('status = ?')
            params.append(status)
        
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        
        query += ' ORDER BY data_publicacao DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def get_destaques(self, limit: int = 5) -> List[Dict]:
        """Busca notícias em destaque"""
        cursor = self.conn.cursor()
        cursor.execute('''
        SELECT * FROM noticias 
        WHERE destaque = 1 AND status = 'publicada'
        ORDER BY data_publicacao DESC 
        LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def update_noticia(self, noticia_id: int, data: Dict) -> Optional[Dict]:
        """Atualiza notícia"""
        try:
            cursor = self.conn.cursor()
            updates = ['data_atualizacao = CURRENT_TIMESTAMP']
            params = []
            
            fields = ['titulo', 'subtitulo', 'conteudo', 'categoria', 'autor',
                     'imagem_url', 'status', 'tags', 'destaque', 'fonte']
            
            for field in fields:
                if field in data:
                    updates.append(f'{field} = ?')
                    params.append(data[field])
            
            # Tratar slug separadamente (só atualizar se fornecido)
            if 'slug' in data and data['slug']:
                slug_custom = data['slug'].strip()
                if slug_custom:
                    # Verificar se o slug já existe para outra notícia
                    cursor.execute('SELECT id FROM noticias WHERE slug = ? AND id != ?', 
                                  (slug_custom, noticia_id))
                    if cursor.fetchone():
                        # Slug já existe, gerar um único
                        noticia = self.get_noticia_by_id(noticia_id)
                        titulo = noticia['titulo'] if noticia else data.get('titulo', '')
                        slug = self._gerar_slug_unico(titulo, slug_custom)
                    else:
                        slug = slug_custom
                    
                    updates.append('slug = ?')
                    params.append(slug)
            
            if updates:
                params.append(noticia_id)
                query = f'UPDATE noticias SET {", ".join(updates)} WHERE id = ?'
                cursor.execute(query, params)
                self.conn.commit()
            
            return self.get_noticia_by_id(noticia_id)
        except Exception as e:
            print(f"Erro ao atualizar notícia: {e}")
            return None
    
    def delete_noticia(self, noticia_id: int) -> bool:
        """Exclui notícia (apenas marca como arquivada)"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('UPDATE noticias SET status = "arquivada" WHERE id = ?',
                          (noticia_id,))
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            print(f"Erro ao excluir notícia: {e}")
            return False
    
    def search_noticias(self, query: str, limit: int = 20) -> List[Dict]:
        """Busca notícias por texto"""
        cursor = self.conn.cursor()
        search_term = f'%{query}%'
        cursor.execute('''
        SELECT * FROM noticias 
        WHERE (titulo LIKE ? OR subtitulo LIKE ? OR conteudo LIKE ? OR tags LIKE ?)
          AND status = 'publicada'
        ORDER BY data_publicacao DESC 
        LIMIT ?
        ''', (search_term, search_term, search_term, search_term, limit))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ========== MÉTODOS DE CATEGORIAS ==========
    
    def get_all_categorias(self) -> List[Dict]:
        """Lista todas as categorias"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM categorias WHERE visivel = 1 ORDER BY ordem, nome')
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def get_categoria_by_nome(self, nome: str) -> Optional[Dict]:
        """Busca categoria por nome"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM categorias WHERE nome = ?', (nome,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_noticias_count_by_categoria(self) -> List[Dict]:
        """Conta notícias por categoria"""
        cursor = self.conn.cursor()
        cursor.execute('''
        SELECT categoria, COUNT(*) as total 
        FROM noticias 
        WHERE status = 'publicada'
        GROUP BY categoria 
        ORDER BY total DESC
        ''')
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ========== MÉTODOS DE INSCRITOS ==========
    
    def inscrever_email(self, email: str, nome: str = "") -> Optional[Dict]:
        """Inscreve um email na newsletter"""
        try:
            cursor = self.conn.cursor()
            
            # Verificar se já está inscrito
            cursor.execute('SELECT * FROM inscritos WHERE email = ?', (email.lower(),))
            existing = cursor.fetchone()
            
            if existing:
                row = dict(existing)
                if row['status'] == 'cancelado':
                    # Reativar inscrição
                    codigo_confirmacao = secrets.token_urlsafe(32)
                    cursor.execute('''
                    UPDATE inscritos 
                    SET status = 'ativo', 
                        confirmado = 0,
                        codigo_confirmacao = ?,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''', (codigo_confirmacao, row['id']))
                    self.conn.commit()
                    return dict(cursor.execute('SELECT * FROM inscritos WHERE id = ?', (row['id'],)).fetchone())
                elif row['confirmado']:
                    return None  # Já está inscrito e confirmado
            
            # Gerar código de confirmação
            codigo_confirmacao = secrets.token_urlsafe(32)
            
            cursor.execute('''
            INSERT OR REPLACE INTO inscritos (email, nome, codigo_confirmacao, confirmado, status)
            VALUES (?, ?, ?, ?, ?)
            ''', (
                email.lower().strip(),
                nome.strip() if nome else "",
                codigo_confirmacao,
                0,  # Não confirmado ainda
                'ativo'
            ))
            
            inscrito_id = cursor.lastrowid
            self.conn.commit()
            
            cursor.execute('SELECT * FROM inscritos WHERE id = ?', (inscrito_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
            
        except Exception as e:
            print(f"Erro ao inscrever email: {e}")
            return None
    
    def confirmar_inscricao(self, codigo: str) -> bool:
        """Confirma uma inscrição via código"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM inscritos WHERE codigo_confirmacao = ?', (codigo,))
            result = cursor.fetchone()
            
            if not result:
                return False
            
            cursor.execute('''
            UPDATE inscritos 
            SET confirmado = 1, 
                codigo_confirmacao = NULL,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE codigo_confirmacao = ?
            ''', (codigo,))
            
            self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            print(f"Erro ao confirmar inscrição: {e}")
            return False
    
    def get_inscrito_by_email(self, email: str) -> Optional[Dict]:
        """Busca inscrito por email"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM inscritos WHERE email = ?', (email.lower(),))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_inscrito_by_codigo(self, codigo: str) -> Optional[Dict]:
        """Busca inscrito por código de confirmação"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM inscritos WHERE codigo_confirmacao = ?', (codigo,))
        row = cursor.fetchone()
        return dict(row) if row else None


    def list_inscritos(self, limit: int = 200, offset: int = 0,
                      status: str = None, confirmado: int = None, q: str = None) -> List[Dict]:
        
        cursor = self.conn.cursor()
        query = "SELECT id, email, nome, confirmado, criado_em, ultimo_envio, total_envios, status FROM inscritos"
        params = []
        conditions = []

        if status:
            conditions.append("status = ?")
            params.append(status)

        if confirmado is not None:
            conditions.append("confirmado = ?")
            params.append(1 if str(confirmado) in ["1", "true", "True"] else 0)

        if q:
            like = f"%{q}%"
            conditions.append("(email LIKE ? OR nome LIKE ?)")
            params.extend([like, like])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY criado_em DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

# Instância global do banco
db = Database()

# =====================================================
# SERVIÇO DE EMAIL
# =====================================================
class EmailService:
    def __init__(self):
        self.config = EMAIL_CONFIG
        self.smtp_server = None
        self.connected = False
    
    def connect(self) -> bool:
        """Conecta ao servidor SMTP"""
        try:
            context = ssl.create_default_context()
            self.smtp_server = smtplib.SMTP(self.config['smtp_server'], self.config['smtp_port'])
            self.smtp_server.starttls(context=context)
            self.smtp_server.login(self.config['sender_email'], self.config['sender_password'])
            self.connected = True
            print(f"✅ Conectado ao SMTP: {self.config['smtp_server']}:{self.config['smtp_port']}")
            return True
        except Exception as e:
            print(f"❌ Erro ao conectar ao servidor SMTP: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Desconecta do servidor SMTP"""
        try:
            if self.smtp_server:
                self.smtp_server.quit()
                print("✅ Desconectado do servidor SMTP")
        except Exception as e:
            print(f"❌ Erro ao desconectar do SMTP: {e}")
        finally:
            self.connected = False
    
    def send_email(self, to_email: str, subject: str, html_content: str, plain_text: str = None) -> bool:
        """Envia um email"""
        if not self.connected and not self.connect():
            print(f"❌ Não conectado ao SMTP para enviar para {to_email}")
            return False
        
        try:
            # Criar mensagem
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{self.config['company_name']} <{self.config['sender_email']}>"
            msg['To'] = to_email
            
            # Adicionar versão texto simples
            if plain_text:
                msg.attach(MIMEText(plain_text, 'plain'))
            else:
                # Extrair texto simples do HTML
                plain = re.sub('<[^<]+?>', '', html_content)
                plain = re.sub('\s+', ' ', plain).strip()
                msg.attach(MIMEText(plain, 'plain'))
            
            # Adicionar versão HTML
            msg.attach(MIMEText(html_content, 'html'))
            
            # Enviar email
            self.smtp_server.send_message(msg)
            print(f"✅ Email enviado para: {to_email}")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao enviar email para {to_email}: {e}")
            return False

# Instância global do serviço de email
email_service = EmailService()

# =====================================================
# DECORADORES DE AUTENTICAÇÃO
# =====================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Não autorizado'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Não autorizado'}), 401
        user = db.get_user_by_id(session['user_id'])
        if not user or user['perfil'] != 'admin':
            return jsonify({'success': False, 'error': 'Acesso restrito a administradores'}), 403
        return f(*args, **kwargs)
    return decorated_function

# =====================================================
# ROTAS DO SISTEMA
# =====================================================

# ========== PÁGINAS ESTÁTICAS ==========
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = STATIC_DIR / filename
    if file_path.exists() and file_path.is_file():
        return send_from_directory(STATIC_DIR, filename)
    
    # Se for uma página HTML que não existe, servir index.html
    if filename.endswith('.html'):
        return send_from_directory(STATIC_DIR, 'index.html')
    
    return jsonify({'success': False, 'error': 'Arquivo não encontrado'}), 404

# ========== API DE AUTENTICAÇÃO ==========
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    senha = data.get('senha', '')
    
    if not email or not senha:
        return jsonify({'success': False, 'error': 'E-mail e senha são obrigatórios'}), 400
    
    user = db.authenticate_user(email, senha)
    if not user:
        return jsonify({'success': False, 'error': 'Credenciais inválidas'}), 401
    
    # Configurar sessão
    session.permanent = True
    session['user_id'] = user['id']
    session['user_email'] = user['email']
    session['user_nome'] = user['nome']
    session['user_perfil'] = user['perfil']
    
    return jsonify({
        'success': True,
        'user': {
            'id': user['id'],
            'nome': user['nome'],
            'email': user['email'],
            'perfil': user['perfil']
        }
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/check-session', methods=['GET'])
@app.route('/api/check-login', methods=['GET'])
def check_session():
    if 'user_id' in session:
        user = db.get_user_by_id(session['user_id'])
        if user and user['status'] == 'active':
            return jsonify({
                'authenticated': True,
                'user': {
                    'id': user['id'],
                    'nome': user['nome'],
                    'email': user['email'],
                    'perfil': user['perfil']
                }
            })
    
    session.clear()
    return jsonify({'authenticated': False})


# ========== API ADMIN: BACKUP/RESTORE DO BANCO ==========
@app.route('/api/admin/db/export', methods=['GET'])
@admin_required
def admin_export_db():
    """Exporta um backup consistente do banco SQLite (apenas admin)."""
    try:
        # Gerar nome do arquivo
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = BACKUP_DIR / f"quartopodernews-backup-{ts}.db"

        # Fazer backup consistente usando a API de backup do SQLite
        dest = sqlite3.connect(backup_path)
        try:
            # checkpoint WAL para reduzir inconsistências
            try:
                db.conn.execute("PRAGMA wal_checkpoint(FULL)")
            except Exception:
                pass
            db.conn.backup(dest)
            dest.commit()
        finally:
            dest.close()

        return send_file(
            backup_path,
            as_attachment=True,
            download_name=backup_path.name,
            mimetype='application/octet-stream'
        )
    except Exception as e:
        print(f"Erro ao exportar banco: {e}")
        return jsonify({'success': False, 'error': 'Erro ao exportar banco'}), 500


@app.route('/api/admin/db/import', methods=['POST'])
@admin_required
def admin_import_db():
    """Importa/restaura o banco SQLite (apenas admin). ATENÇÃO: sobrescreve o banco atual."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Arquivo não enviado'}), 400

        f = request.files['file']
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'Arquivo inválido'}), 400

        # Aceitar apenas .db/.sqlite para este projeto (evita restaurar formatos inesperados)
        filename = secure_filename(f.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in ('db', 'sqlite', 'sqlite3'):
            return jsonify({'success': False, 'error': 'Formato não suportado. Envie um arquivo .db/.sqlite'}), 400

        # Salvar em arquivo temporário
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        temp_path = BACKUP_DIR / f"import-temp-{ts}.{ext}"
        f.save(temp_path)

        # Validar se é um SQLite abrindo rapidamente
        try:
            test = sqlite3.connect(temp_path)
            test.execute("SELECT name FROM sqlite_master LIMIT 1;")
            test.close()
        except Exception:
            try:
                test.close()
            except Exception:
                pass
            temp_path.unlink(missing_ok=True)
            return jsonify({'success': False, 'error': 'Arquivo não parece ser um banco SQLite válido'}), 400

        # Fazer backup do banco atual antes de substituir
        current_backup = BACKUP_DIR / f"pre-import-backup-{ts}.db"
        try:
            dest = sqlite3.connect(current_backup)
            try:
                try:
                    db.conn.execute("PRAGMA wal_checkpoint(FULL)")
                except Exception:
                    pass
                db.conn.backup(dest)
                dest.commit()
            finally:
                dest.close()
        except Exception as e:
            print(f"⚠️  Não foi possível criar backup pré-import: {e}")

        # Fechar conexão atual do singleton e substituir arquivo
        try:
            db.conn.close()
        except Exception:
            pass

        shutil.copy2(temp_path, DB_PATH)

        # Reabrir conexão e re-inicializar
        Database._instance = None
        globals()['db'] = Database()

        # Limpar arquivo temporário
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

        return jsonify({'success': True, 'message': 'Banco restaurado com sucesso'}), 200

    except Exception as e:
        print(f"Erro ao importar banco: {e}")
        return jsonify({'success': False, 'error': 'Erro ao restaurar banco'}), 500


# ========== API DE NOTÍCIAS COM SLUG ==========
@app.route('/api/noticias', methods=['GET'])
def list_noticias():
    limit = request.args.get('limit', default=20, type=int)
    offset = request.args.get('offset', default=0, type=int)
    categoria = request.args.get('categoria', type=str)
    status = request.args.get('status', type=str)
    
    noticias = db.get_all_noticias(limit, offset, categoria, status)
    
    # Formatar datas para exibição
    for noticia in noticias:
        if noticia.get('data_publicacao'):
            try:
                data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
                noticia['data_formatada'] = data_obj.strftime('%d/%m/%Y às %H:%M')
                noticia['data_relativa'] = data_obj.strftime('%d %b')
            except:
                noticia['data_formatada'] = 'Hoje'
                noticia['data_relativa'] = 'Hoje'
    
    return jsonify({'success': True, 'noticias': noticias, 'total': len(noticias)})

@app.route('/api/noticias/<int:noticia_id>', methods=['GET'])
def get_noticia(noticia_id):
    """Busca notícia por ID"""
    noticia = db.get_noticia_by_id(noticia_id)
    if not noticia:
        return jsonify({'success': False, 'error': 'Notícia não encontrada'}), 404
    
    # Formatar data
    if noticia.get('data_publicacao'):
        try:
            data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
            noticia['data_formatada'] = data_obj.strftime('%d/%m/Y às %H:%M')
        except:
            noticia['data_formatada'] = 'Hoje'
    
    return jsonify({'success': True, 'noticia': noticia})

@app.route('/api/noticias/slug/<slug>', methods=['GET'])
def get_noticia_by_slug(slug):
    """Busca notícia por slug"""
    try:
        noticia = db.get_noticia_by_slug(slug)
        if not noticia:
            return jsonify({'success': False, 'error': 'Notícia não encontrada'}), 404
        
        # Formatar data
        if noticia.get('data_publicacao'):
            try:
                data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
                noticia['data_formatada'] = data_obj.strftime('%d/%m/Y às %H:%M')
            except:
                noticia['data_formatada'] = 'Hoje'
        
        return jsonify({'success': True, 'noticia': noticia})
        
    except Exception as e:
        print(f"Erro ao buscar notícia por slug: {str(e)}")
        return jsonify({'success': False, 'error': 'Erro interno'}), 500

@app.route('/api/noticias', methods=['POST'])
@login_required
def create_noticia():
    """Cria nova notícia com suporte a slug"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'Dados não fornecidos'}), 400
        
        # Mapear campos do frontend para o backend
        mapped_data = {
            'titulo': data.get('titulo', '').strip(),
            'subtitulo': data.get('chamada', data.get('subtitulo', '')).strip(),
            'conteudo': data.get('conteudo', '').strip(),
            'categoria': data.get('categoria', '').strip(),
            'autor': data.get('autor', '').strip(),
            'imagem_url': data.get('imagem', data.get('imagem_url', '')).strip(),
            'status': 'publicada' if data.get('liberada') == 'sim' else 'rascunho',
            'tags': data.get('tags', '').strip(),
            'destaque': bool(data.get('destaque', False)),
            'fonte': data.get('fonte', 'Quarto Poder News').strip(),
            'slug': data.get('slug', '').strip(),  # Campo slug
            'autor_id': session.get('user_id')
        }
        
        # Validar campos obrigatórios
        required_fields = ['titulo', 'conteudo', 'categoria', 'autor']
        missing_fields = [field for field in required_fields if not mapped_data.get(field)]
        
        if missing_fields:
            return jsonify({
                'success': False, 
                'error': f'Campos obrigatórios faltando: {", ".join(missing_fields)}'
            }), 400
        
        # Se não tem autor, usar usuário logado
        if not mapped_data['autor']:
            user = db.get_user_by_id(session['user_id'])
            if user:
                mapped_data['autor'] = user['nome']
        
        # Criar notícia no banco
        noticia = db.create_noticia(mapped_data)
        if not noticia:
            return jsonify({'success': False, 'error': 'Erro ao criar notícia no banco de dados'}), 500
        
        return jsonify({'success': True, 'noticia': noticia}), 201
        
    except Exception as e:
        print(f"Erro na criação de notícia: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Erro interno: {str(e)}'}), 500

@app.route('/api/noticias/<int:noticia_id>', methods=['PUT'])
@login_required
def update_noticia(noticia_id):
    """Atualiza notícia incluindo slug"""
    noticia = db.get_noticia_by_id(noticia_id)
    if not noticia:
        return jsonify({'success': False, 'error': 'Notícia não encontrada'}), 404
    
    # Verificar permissões (apenas admin pode editar)
    user = db.get_user_by_id(session['user_id'])
    if user['perfil'] != 'admin':
        return jsonify({'success': False, 'error': 'Sem permissão para editar esta notícia'}), 403
    
    data = request.get_json()
    
    # Mapear dados para atualização
    update_data = {}
    
    fields_mapping = {
        'titulo': 'titulo',
        'chamada': 'subtitulo',
        'conteudo': 'conteudo',
        'categoria': 'categoria',
        'autor': 'autor',
        'imagem': 'imagem_url',
        'imagem_url': 'imagem_url',
        'liberada': 'status',
        'tags': 'tags',
        'destaque': 'destaque',
        'fonte': 'fonte',
        'slug': 'slug'
    }
    
    for frontend_field, backend_field in fields_mapping.items():
        if frontend_field in data:
            if frontend_field == 'liberada':
                update_data[backend_field] = 'publicada' if data[frontend_field] == 'sim' else 'rascunho'
            elif frontend_field in ['destaque']:
                value = data[frontend_field]
                if isinstance(value, str):
                    update_data[backend_field] = value.lower() in ['true', '1', 'yes', 'sim']
                else:
                    update_data[backend_field] = bool(value)
            else:
                update_data[backend_field] = data[frontend_field]
    
    updated = db.update_noticia(noticia_id, update_data)
    
    if not updated:
        return jsonify({'success': False, 'error': 'Erro ao atualizar notícia'}), 500
    
    return jsonify({'success': True, 'noticia': updated})

@app.route('/api/noticias/<int:noticia_id>', methods=['DELETE'])
@admin_required
def delete_noticia(noticia_id):
    success = db.delete_noticia(noticia_id)
    if not success:
        return jsonify({'success': False, 'error': 'Erro ao excluir notícia'}), 500
    return jsonify({'success': True})

@app.route('/api/noticias/destaques', methods=['GET'])
def get_destaques():
    limit = request.args.get('limit', default=5, type=int)
    destaques = db.get_destaques(limit)
    
    # Formatar datas
    for noticia in destaques:
        if noticia.get('data_publicacao'):
            try:
                data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
                noticia['data_formatada'] = data_obj.strftime('%d/%m/%Y')
            except:
                noticia['data_formatada'] = 'Hoje'
    
    return jsonify({'success': True, 'destaques': destaques})
# ========== API DE UPLOAD DE IMAGENS ==========
@app.route('/api/upload/image', methods=['POST'])
@login_required
def upload_image():
    """Upload de imagem para notícias"""
    try:
        # Verificar se o arquivo foi enviado
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'Nenhuma imagem enviada'}), 400
        
        file = request.files['image']
        
        # Verificar se o arquivo tem nome
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Nenhuma imagem selecionada'}), 400
        
        # Verificar se é um arquivo de imagem válido
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Tipo de arquivo não permitido. Use PNG, JPG, JPEG, GIF ou WEBP'}), 400
        
        # Verificar se é realmente uma imagem
        file_bytes = file.read(1024)
        file.seek(0)
        
        if not imghdr.what(None, h=file_bytes):
            return jsonify({'success': False, 'error': 'Arquivo não é uma imagem válida'}), 400
        
        # Gerar nome único para o arquivo
        original_filename = secure_filename(file.filename)
        file_extension = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'jpg'
        
        # Usar timestamp + UUID para evitar conflitos
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        new_filename = f"{timestamp}_{unique_id}.{file_extension}"
        
        # Salvar o arquivo
        file_path = UPLOAD_FOLDER / new_filename
        file.save(file_path)
        
        # URL para acessar a imagem
        image_url = f"/static/uploads/{new_filename}"
        
        return jsonify({
            'success': True,
            'message': 'Imagem enviada com sucesso!',
            'image_url': image_url,
            'filename': new_filename
        })
        
    except Exception as e:
        print(f"Erro no upload da imagem: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Erro ao fazer upload: {str(e)}'}), 500

# ========== SERVIR ARQUIVOS DE UPLOAD ==========
@app.route('/static/uploads/<filename>')
def serve_upload(filename):
    """Serve arquivos de upload"""
    try:
        return send_from_directory(UPLOAD_FOLDER, filename)
    except:
        return jsonify({'success': False, 'error': 'Imagem não encontrada'}), 404
# ========== API PARA PÁGINAS PÚBLICAS ==========
@app.route('/api/public/noticias', methods=['GET'])
def public_noticias():
    """API pública para o site - apenas notícias publicadas"""
    limit = request.args.get('limit', default=10, type=int)
    offset = request.args.get('offset', default=0, type=int)
    categoria = request.args.get('categoria', type=str)
    
    # Sempre filtrar apenas notícias publicadas para o público
    noticias = db.get_all_noticias(
        limit=limit, 
        offset=offset, 
        categoria=categoria,
        status='publicada'
    )
    
    # Formatar datas para exibição
    for noticia in noticias:
        if noticia.get('data_publicacao'):
            try:
                data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
                noticia['data_formatada'] = data_obj.strftime('%d/%m/Y às %H:%M')
                noticia['data_relativa'] = data_obj.strftime('%d %b')
            except:
                noticia['data_formatada'] = 'Hoje'
                noticia['data_relativa'] = 'Hoje'
    
    return jsonify({'success': True, 'noticias': noticias, 'total': len(noticias)})

@app.route('/api/public/destaques', methods=['GET'])
def public_destaques():
    """Destaques para a página inicial"""
    destaques = db.get_destaques(limit=5)
    
    for noticia in destaques:
        if noticia.get('data_publicacao'):
            try:
                data_obj = datetime.fromisoformat(noticia['data_publicacao'].replace('Z', '+00:00'))
                noticia['data_formatada'] = data_obj.strftime('%d/%m/%Y')
            except:
                noticia['data_formatada'] = 'Hoje'
    
    return jsonify({'success': True, 'destaques': destaques})

@app.route('/api/public/categorias', methods=['GET'])
def public_categorias():
    """Categorias para navegação"""
    categorias = db.get_all_categorias()
    counts = db.get_noticias_count_by_categoria()
    
    # Adicionar contagem de notícias a cada categoria
    count_dict = {item['categoria']: item['total'] for item in counts}
    for cat in categorias:
        cat['total_noticias'] = count_dict.get(cat['nome'], 0)
    
    return jsonify({'success': True, 'categorias': categorias})



# ========== API PÚBLICA: CONTATO ==========
@app.route('/api/public/contato', methods=['POST'])
def public_contato():
    """Recebe mensagens do formulário de contato (página pública) e encaminha para o email oficial."""
    try:
        data = request.get_json() or {}
        nome = (data.get('nome') or '').strip()
        email = (data.get('email') or '').strip().lower()
        assunto = (data.get('assunto') or '').strip()
        mensagem = (data.get('mensagem') or '').strip()

        if not nome or not email or not assunto or not mensagem:
            return jsonify({'success': False, 'error': 'Preencha nome, email, assunto e mensagem'}), 400

        # Validar email
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            return jsonify({'success': False, 'error': 'Email inválido'}), 400

        # Montar email para a redação (destino = email oficial configurado)
        to_email = EMAIL_CONFIG.get('sender_email', 'quartopodernews.sup1@gmail.com')
        subject = f"📩 Contato do site: {assunto}"

        # HTML simples (seguro)
        html_content = f'''
        <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222">
          <h2 style="margin:0 0 10px 0;color:#003366">Mensagem recebida pelo formulário de contato</h2>
          <p><strong>Nome:</strong> {nome}</p>
          <p><strong>Email:</strong> {email}</p>
          <p><strong>Assunto:</strong> {assunto}</p>
          <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
          <p style="white-space:pre-wrap;margin:0">{mensagem}</p>
          <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
          <p style="font-size:12px;color:#666">Enviado em {datetime.now().strftime("%d/%m/%Y %H:%M")} via Quarto Poder News</p>
        </div>
        '''

        # Enviar usando o serviço já existente
        sent = False
        try:
            if email_service.connect():
                sent = email_service.send_email(to_email=to_email, subject=subject, html_content=html_content)
                email_service.disconnect()
        except Exception as e:
            print(f"Erro ao enviar contato: {e}")
            sent = False

        if not sent:
            return jsonify({'success': False, 'error': 'Não foi possível enviar sua mensagem agora. Tente novamente mais tarde.'}), 500

        return jsonify({'success': True, 'message': 'Mensagem enviada com sucesso'}), 200

    except Exception as e:
        print(f"Erro no endpoint de contato: {e}")
        return jsonify({'success': False, 'error': 'Erro ao processar mensagem'}), 500


# ========== API DE CATEGORIAS ==========
@app.route('/api/categorias', methods=['GET'])
def list_categorias():
    categorias = db.get_all_categorias()
    counts = db.get_noticias_count_by_categoria()
    
    # Adicionar contagem de notícias a cada categoria
    count_dict = {item['categoria']: item['total'] for item in counts}
    for cat in categorias:
        cat['total_noticias'] = count_dict.get(cat['nome'], 0)
    
    return jsonify({'success': True, 'categorias': categorias})

# ========== API DE NEWSLETTER ==========
@app.route('/api/newsletter/inscrever', methods=['POST'])
def inscrever_newsletter():
    """Inscreve um email na newsletter e envia email automático de confirmação"""
    try:
        data = request.get_json()
        email = data.get('email', '').strip().lower()
        nome = data.get('nome', '').strip()
        
        if not email:
            return jsonify({'success': False, 'error': 'Email é obrigatório'}), 400
        
        # Validar formato do email
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            return jsonify({'success': False, 'error': 'Email inválido'}), 400
        
        # Inscrever email no banco
        inscrito = db.inscrever_email(email, nome)
        
        if inscrito is None:
            return jsonify({
                'success': True,
                'message': '🎉 Este email já está inscrito!',
                'email': email,
                'ja_inscrito': True
            })
        
        # ENVIAR EMAIL DE CONFIRMAÇÃO AUTOMÁTICO
        email_enviado = False
        try:
            # Conectar ao serviço de email
            if email_service.connect():
                # Preparar conteúdo do email
                subject = f"🎉 Confirmação de Inscrição - {EMAIL_CONFIG['company_name']}"
                
                # HTML do email (simples e limpo)
                html_content = f'''
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Confirmação de Inscrição</title>
                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            line-height: 1.6;
                            color: #333;
                            margin: 0;
                            padding: 0;
                        }}
                        .container {{
                            max-width: 600px;
                            margin: 0 auto;
                            padding: 20px;
                            background-color: #f9f9f9;
                        }}
                        .header {{
                            background-color: #003366;
                            color: white;
                            padding: 20px;
                            text-align: center;
                            border-radius: 5px 5px 0 0;
                        }}
                        .header h1 {{
                            margin: 0;
                            font-size: 24px;
                        }}
                        .content {{
                            background-color: white;
                            padding: 30px;
                            border-radius: 0 0 5px 5px;
                            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                        }}
                        .welcome {{
                            font-size: 20px;
                            color: #003366;
                            margin-bottom: 20px;
                        }}
                        .message {{
                            font-size: 16px;
                            margin-bottom: 25px;
                        }}
                        .highlight {{
                            background-color: #e8f4fd;
                            border-left: 4px solid #003366;
                            padding: 15px;
                            margin: 20px 0;
                        }}
                        .footer {{
                            margin-top: 30px;
                            padding-top: 20px;
                            border-top: 1px solid #eee;
                            text-align: center;
                            font-size: 14px;
                            color: #666;
                        }}
                        .logo {{
                            font-size: 18px;
                            font-weight: bold;
                            color: #003366;
                            margin-bottom: 10px;
                        }}
                        .cta {{
                            display: inline-block;
                            background-color: #003366;
                            color: white;
                            padding: 12px 25px;
                            text-decoration: none;
                            border-radius: 5px;
                            font-weight: bold;
                            margin: 15px 0;
                        }}
                    </style>
                </head>
                <body>
                    <div class="container">
                        <div class="header">
                            <h1>📰 {EMAIL_CONFIG['company_name']}</h1>
                            <p>Sua fonte confiável de notícias</p>
                        </div>
                        
                        <div class="content">
                            <div class="welcome">
                                Olá{nome if nome else ''}!
                            </div>
                            
                            <div class="message">
                                <p>É com grande satisfação que confirmamos sua inscrição na nossa newsletter!</p>
                                <p>A partir de agora, você receberá as principais notícias e destaques diretamente no seu email.</p>
                            </div>
                            
                            <div class="highlight">
                                <p><strong>📧 Email cadastrado:</strong> {email}</p>
                                <p><strong>📅 Data da inscrição:</strong> {datetime.now().strftime("%d/%m/%Y às %H:%M")}</p>
                            </div>
                            
                            <div class="message">
                                <p>Nossa equipe trabalha diariamente para trazer as notícias mais relevantes e atualizadas.</p>
                                <p>Fique atento à sua caixa de entrada!</p>
                            </div>
                            
                            <center>
                                <a href="#" class="cta">Acessar Site</a>
                            </center>
                        </div>
                        
                        <div class="footer">
                            <div class="logo">Quarto Poder News</div>
                            <p>Sua fonte confiável de informação 24h</p>
                            <p>📍 Rua das Notícias, 123 - Centro</p>
                            <p>📞 (11) 99999-9999 | ✉️ contato@quartopodernews.com</p>
                            <p style="font-size: 12px; color: #999; margin-top: 20px;">
                                Você está recebendo este email porque se inscreveu em nosso site.<br>
                                Para cancelar a inscrição, responda este email com o assunto "Cancelar".
                            </p>
                        </div>
                    </div>
                </body>
                </html>
                '''
                
                # Versão em texto simples
                plain_text = f"""
                Confirmação de Inscrição - {EMAIL_CONFIG['company_name']}
                
                Olá{nome if nome else ''}!
                
                É com grande satisfação que confirmamos sua inscrição na nossa newsletter!
                A partir de agora, você receberá as principais notícias e destaques diretamente no seu email.
                
                📧 Email cadastrado: {email}
                📅 Data da inscrição: {datetime.now().strftime("%d/%m/%Y às %H:%M")}
                
                Nossa equipe trabalha diariamente para trazer as notícias mais relevantes e atualizadas.
                Fique atento à sua caixa de entrada!
                
                Atenciosamente,
                
                {EMAIL_CONFIG['company_name']}
                Sua fonte confiável de informação 24h
                """
                
                # Enviar email
                email_enviado = email_service.send_email(
                    to_email=email,
                    subject=subject,
                    html_content=html_content,
                    plain_text=plain_text
                )
                
                if email_enviado:
                    print(f"✅ Email de confirmação enviado para: {email}")
                else:
                    print(f"⚠️ Falha ao enviar email de confirmação para: {email}")
                
                # Desconectar
                email_service.disconnect()
            else:
                print(f"⚠️ Não foi possível conectar ao servidor de email para: {email}")
                
        except Exception as email_error:
            print(f"❌ Erro ao enviar email de confirmação: {email_error}")
            # Não falha a inscrição se o email falhar
        
        return jsonify({
            'success': True,
            'message': '✅ Inscrição realizada com sucesso!' + (' Confirmação enviada por email.' if email_enviado else ''),
            'email': email,
            'nome': nome if nome else None,
            'email_enviado': email_enviado
        })
            
    except Exception as e:
        print(f"❌ Erro na inscrição: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Erro ao processar inscrição'}), 500

@app.route('/api/newsletter/confirmar/<codigo>', methods=['GET'])
def confirmar_inscricao(codigo):
    """Confirma uma inscrição via código (manter para compatibilidade)"""
    success = db.confirmar_inscricao(codigo)
    if success:
        return jsonify({'success': True, 'message': '✅ Inscrição confirmada com sucesso!'})
    return jsonify({'success': False, 'error': 'Código de confirmação inválido'}), 400


# ========== API DE USUÁRIOS ==========
@app.route('/api/usuarios', methods=['GET'])
@admin_required
def api_list_usuarios():
    usuarios = db.list_usuarios()
    return jsonify({'success': True, 'usuarios': usuarios, 'total': len(usuarios)})

@app.route('/api/usuarios', methods=['POST'])
@admin_required
def api_create_usuario():
    data = request.get_json() or {}
    user = db.create_usuario(data)
    if not user:
        return jsonify({'success': False, 'error': 'Dados inválidos ou email já cadastrado'}), 400
    # Não devolver senha_hash
    user.pop('senha_hash', None)
    return jsonify({'success': True, 'usuario': user}), 201

@app.route('/api/usuarios/<int:user_id>', methods=['PUT'])
@admin_required
def api_update_usuario(user_id):
    data = request.get_json() or {}
    # Não permitir editar o próprio perfil para evitar lockout acidental
    if session.get('user_id') == user_id and 'status' in data and data.get('status') == 'inactive':
        return jsonify({'success': False, 'error': 'Você não pode desativar seu próprio usuário'}), 400

    updated = db.update_usuario(user_id, data)
    if not updated:
        return jsonify({'success': False, 'error': 'Erro ao atualizar usuário'}), 400
    updated.pop('senha_hash', None)
    return jsonify({'success': True, 'usuario': updated})

@app.route('/api/usuarios/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_usuario(user_id):
    # Bloquear exclusão do próprio usuário
    if session.get('user_id') == user_id:
        return jsonify({'success': False, 'error': 'Você não pode excluir seu próprio usuário'}), 400
    ok = db.delete_usuario(user_id)
    if not ok:
        return jsonify({'success': False, 'error': 'Erro ao excluir usuário'}), 400
    return jsonify({'success': True})

@app.route('/api/usuarios/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def api_toggle_usuario_status(user_id):
    if session.get('user_id') == user_id:
        return jsonify({'success': False, 'error': 'Você não pode alterar o status do seu próprio usuário'}), 400
    updated = db.toggle_usuario_status(user_id)
    if not updated:
        return jsonify({'success': False, 'error': 'Erro ao atualizar status'}), 400
    updated.pop('senha_hash', None)
    return jsonify({'success': True, 'usuario': updated})

# ========== API/ÁREA RESTRITA: INSCRITOS ==========
@app.route('/api/inscritos', methods=['GET'])
@login_required
def api_list_inscritos():
    # admin ou jornalista
    user = db.get_user_by_id(session['user_id'])
    if not user or user.get('perfil') not in ('admin', 'jornalista'):
        return jsonify({'success': False, 'error': 'Acesso restrito'}), 403

    limit = request.args.get('limit', default=200, type=int)
    offset = request.args.get('offset', default=0, type=int)
    status = request.args.get('status', type=str)
    confirmado = request.args.get('confirmado', default=None, type=str)
    q = request.args.get('q', type=str)

    inscritos = db.list_inscritos(limit=limit, offset=offset, status=status, confirmado=confirmado, q=q)
    return jsonify({'success': True, 'inscritos': inscritos, 'total': len(inscritos)})

@app.route('/area/inscritos', methods=['GET'])
def area_inscritos_page():
    if 'user_id' not in session:
        return redirect('login.html')
    # admin ou jornalista
    user = db.get_user_by_id(session['user_id'])
    if not user or user.get('perfil') not in ('admin', 'jornalista'):
        return redirect('admin.html')
    return send_from_directory(STATIC_DIR, 'inscritos.html')


# ========== HEALTH CHECK ==========
@app.route('/api/health', methods=['GET'])
def health_check():
    try:
        # Verificar conexão com banco
        cursor = db.conn.cursor()
        cursor.execute('SELECT 1')
        
        # Testar conexão de email
        email_connected = email_service.connect()
        if email_connected:
            email_service.disconnect()
        
        # Verificar se a coluna slug existe
        cursor.execute("PRAGMA table_info(noticias)")
        columns = cursor.fetchall()
        column_names = [col[1] for col in columns]
        slug_exists = 'slug' in column_names
        
        # Verificar se tem constraint UNIQUE
        cursor.execute("PRAGMA index_list(noticias)")
        indexes = cursor.fetchall()
        unique_indexes = [idx[1] for idx in indexes if idx[2] == 1]
        slug_unique = any('slug' in idx.lower() for idx in unique_indexes)
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'database': 'connected',
            'email_service': 'connected' if email_connected else 'disconnected',
            'schema': {
                'slug_column_exists': slug_exists,
                'slug_unique_constraint': slug_unique,
                'tables': {
                    'usuarios': db.conn.execute('SELECT COUNT(*) as c FROM usuarios').fetchone()['c'],
                    'noticias': db.conn.execute('SELECT COUNT(*) as c FROM noticias').fetchone()['c'],
                    'categorias': db.conn.execute('SELECT COUNT(*) as c FROM categorias').fetchone()['c'],
                    'inscritos': db.conn.execute('SELECT COUNT(*) as c FROM inscritos WHERE status = "ativo" AND confirmado = 1').fetchone()['c']
                }
            }
        })
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

# =====================================================
# INICIALIZAÇÃO
# =====================================================
if __name__ == '__main__':
    print("=" * 60)
    print("🚀 QUARTO PODER NEWS - SERVIDOR INICIADO")
    print("=" * 60)
    print(f"📁 Banco de dados: {DB_PATH}")
    print(f"📧 Serviço de Email: Gmail SMTP")
    print(f"📧 Email remetente: {EMAIL_CONFIG['sender_email']}")
    print(f"📧 Confirmação automática: ATIVADA")
    print(f"🌐 URL: http://127.0.0.1:5000")
    print(f"👤 Admin: admin@quartopodernews.com / admin123")
    print("=" * 60)
    print("📋 Endpoints principais:")
    print("  • /api/login (POST) - Login")
    print("  • /api/logout (POST) - Logout")
    print("  • /api/check-session (GET) - Verificar sessão")
    print("  • /api/newsletter/inscrever (POST) - Inscrever na newsletter + EMAIL AUTOMÁTICO")
    print("  • /api/noticias (CRUD) - Gerenciar notícias COM SLUG")
    print("  • /api/noticias/slug/<slug> (GET) - Buscar notícia por slug")
    print("  • /api/public/noticias (GET) - Notícias públicas")
    print("  • /api/public/destaques (GET) - Destaques para site")
    print("  • /api/public/categorias (GET) - Categorias públicas")
    print("=" * 60)
    print("📧 Sistema de Email Automático:")
    print("  ✓ Inscrição gera confirmação instantânea")
    print("  ✓ Email enviado via Gmail SMTP")
    print("  ✓ Template HTML profissional")
    print("  ✓ Confirmação automática no banco")
    print("=" * 60)
    print("📰 Banco de dados limpo:")
    print("  ✓ Nenhuma notícia estática")
    print("  ✓ Apenas categorias e usuário admin")
    print("  ✓ Você pode criar suas próprias notícias")
    print("=" * 60)
    
    # Testar conexão de email no início
    print("🔧 Testando conexão com servidor de email...")
    if email_service.connect():
        print("✅ Conexão com email estabelecida com sucesso!")
        email_service.disconnect()
    else:
        print("⚠️  Atenção: Não foi possível conectar ao servidor de email.")
        print("   Verifique as credenciais no arquivo de configuração.")
    
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)