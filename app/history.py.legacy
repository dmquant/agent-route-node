import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(__file__), 'logs.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS historical_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            agent TEXT,
            status TEXT,
            timestamp INTEGER,
            fullContent TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_log(title: str, agent: str, status: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    timestamp = int(time.time() * 1000)
    c.execute(
        'INSERT INTO historical_logs (title, agent, status, timestamp, fullContent) VALUES (?, ?, ?, ?, ?)',
        (title, agent, status, timestamp, content)
    )
    conn.commit()
    conn.close()

def get_logs():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM historical_logs ORDER BY timestamp DESC LIMIT 50')
    rows = c.fetchall()
    conn.close()
    
    logs = []
    now_ms = int(time.time() * 1000)
    for r in rows:
        diff_mins = (now_ms - r['timestamp']) // 60000
        if diff_mins == 0:
            time_ago = "Just now"
        elif diff_mins < 60:
            time_ago = f"{diff_mins} mins ago"
        else:
            time_ago = f"{diff_mins // 60} hours ago"
        logs.append({
            "id": r['id'],
            "title": r['title'],
            "agent": r['agent'],
            "status": r['status'],
            "timeAgo": time_ago,
            "fullContent": r['fullContent']
        })
    return logs
