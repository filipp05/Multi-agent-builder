"""SQLite transactions own version checks and idempotency, never the model."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

class Conflict(ValueError): pass
class NotFound(ValueError): pass

def encode(value): return json.dumps(value,ensure_ascii=False,sort_keys=True)

class Repository:
    def list_events(self,project_id):
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(x) for x in db.execute('SELECT id,kind,version,created_at FROM events WHERE project_id=? ORDER BY id',(project_id,))]

    def __init__(self,path):
        self.path=str(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands(scope TEXT, action_id TEXT, hash TEXT, response TEXT, PRIMARY KEY(scope,action_id));
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, project_id TEXT, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project_id TEXT, status TEXT, input_version INTEGER, metadata TEXT);
            CREATE TABLE IF NOT EXISTS attempts(run_id TEXT, attempt INTEGER, metadata TEXT, PRIMARY KEY(run_id,attempt));
            CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY, project_id TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS drafts(project_id TEXT, version INTEGER, content TEXT, PRIMARY KEY(project_id,version));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,project_id TEXT,kind TEXT,version INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()

    def _get(self,db,pid):
        row=db.execute('SELECT snapshot FROM projects WHERE id=?',(pid,)).fetchone()
        if not row: raise NotFound('Проект не найден')
        return json.loads(row[0])

    def get_project(self,pid):
        with self.connect() as db: return self._get(db,pid)

    def list_projects(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT snapshot FROM projects ORDER BY rowid DESC')]

    def _save(self,db,s,kind):
        db.execute('INSERT OR REPLACE INTO projects VALUES(?,?)',(s['project_id'],encode(s)))
        db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(s['project_id'],s['state_version'],encode(s['draft'])))
        for item in s['inputs']:
            db.execute('INSERT OR IGNORE INTO inputs VALUES(?,?,?)',(item['id'],s['project_id'],encode(item)))
        if s['run_id']:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, metadata=excluded.metadata',
                (s['run_id'],s['project_id'],s['status'],s.get('input_version'),encode(s.get('model_metadata'))))
            meta=s.get('model_metadata')
            if meta:
                for attempt in meta.get('attempts',[meta]):
                    db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(s['run_id'],attempt.get('attempt',1),encode(attempt)))
        if s.get('artifact'):
            doc=s['artifact']
            prior=db.execute('SELECT document FROM artifacts WHERE id=?',(doc['artifact_id'],)).fetchone()
            if prior and prior[0]!=encode(doc): raise Conflict('Документ неизменяем')
            db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?)',(doc['artifact_id'],s['project_id'],encode(doc)))
        db.execute('INSERT INTO events(project_id,kind,version) VALUES(?,?,?)',(s['project_id'],kind,s['state_version']))

    def command(self,pid,action_id,expected_version,payload,transform):
        scope=pid or 'create'
        digest=hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT hash,response FROM commands WHERE scope=? AND action_id=?',(scope,action_id)).fetchone()
            if prior:
                if prior[0]!=digest: raise Conflict('Этот идентификатор действия уже использован с другими данными')
                return json.loads(prior[1]),False
            s=self._get(db,pid) if pid else None
            if s and s['state_version']!=expected_version: raise Conflict('Сводка изменилась. Обновите страницу')
            old_run=s.get('run_id') if s else None
            new=transform(deepcopy(s))
            if old_run and old_run!=new.get('run_id'):
                db.execute('UPDATE runs SET status=? WHERE id=?',('superseded',old_run))
            self._save(db,new,payload['kind'])
            db.execute('INSERT INTO commands VALUES(?,?,?,?)',(scope,action_id,digest,encode(new)))
            return new,True

    def update_run(self,pid,run_id,input_version,transform):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            s=self._get(db,pid)
            if s['run_id']!=run_id or s['input_version']!=input_version or s['status'] not in ('queued','running'):
                return False
            s=transform(s)
            s['state_version']+=1
            self._save(db,s,'model_result')
            return True

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=list(db.execute('SELECT snapshot FROM projects'))
            count=0
            for row in rows:
                s=json.loads(row[0])
                if s['status'] in ('queued','running'):
                    s['status']='interrupted';s['state_version']+=1
                    s['error']={'code':'interrupted','message':'Расчёт прерван. Ввод сохранён; повторите вручную.'}
                    self._save(db,s,'recovered'); count+=1
            return count
