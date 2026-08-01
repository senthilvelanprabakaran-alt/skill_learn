from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
import mysql.connector
import jwt
import bcrypt
import datetime
import requests
import os
import json
import urllib.parse
from functools import wraps

app = Flask(__name__)
CORS(app)

# ─── CONFIG ────────────────────────────────────────────────
app.config['SECRET_KEY'] = 'skillforge_secret_key_change_in_production'
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', 'gsk_....')
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', ''..........')  # Optional

# Groq models tried in order — skips on 429/503
GROQ_MODELS = [
    'llama-3.1-8b-instant',
    'llama3-8b-8192',
    'gemma2-9b-it',
    'mixtral-8x7b-32768',
]

def openrouter_chat(messages_payload, max_tokens=800, temperature=0.7):
    """Try each Groq model in order; skip on rate-limit or error."""
    for model in GROQ_MODELS:
        try:
            resp = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {GROQ_API_KEY}',
                    'Content-Type': 'application/json',
                },
                json={'model': model, 'messages': messages_payload,
                      'max_tokens': max_tokens, 'temperature': temperature},
                timeout=30
            )
            data = resp.json()
            print(f"[Groq] Model: {model} | Status: {resp.status_code}")
            if resp.status_code in (429, 503) or 'error' in data:
                print(f"[Groq] Skipping {model}: {data.get('error', {}).get('message', '')}")
                continue
            if 'choices' in data and data['choices']:
                return data['choices'][0]['message']['content'], None
        except Exception as e:
            print(f"[Groq] Exception with {model}: {e}")
            continue
    return None, "All Groq models are currently unavailable. Please try again in a moment."


DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', 'root123'),
    'database': 'skillforge_db',
    'autocommit': True
}

# ─── DB HELPER ──────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def query_db(sql, params=(), fetchone=False, commit=False):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params)
    if commit:
        conn.commit()
        result = cursor.lastrowid
    elif fetchone:
        result = cursor.fetchone()
    else:
        result = cursor.fetchall()
    cursor.close()
    conn.close()
    return result

# ─── JWT AUTH DECORATOR ─────────────────────────────────────
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = query_db('SELECT * FROM users WHERE id = %s', (data['user_id'],), fetchone=True)
            if not current_user:
                return jsonify({'error': 'User not found'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

# ─── ROUTES: PAGES ──────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/learn/<skill>')
def learn(skill):
    return render_template('learn.html', skill=skill)

@app.route('/profile')
def profile():
    return render_template('profile.html')

@app.route('/performance')
def performance():
    return render_template('performance.html')
@app.route('/challenges')
def challenges():
    return render_template('challenges.html')

# ─── AUTH ───────────────────────────────────────────────────
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not all([name, email, password]):
        return jsonify({'error': 'All fields required'}), 400

    existing = query_db('SELECT id FROM users WHERE email = %s', (email,), fetchone=True)
    if existing:
        return jsonify({'error': 'Email already registered'}), 409

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = query_db(
        'INSERT INTO users (name, email, password_hash, created_at) VALUES (%s, %s, %s, NOW())',
        (name, email, hashed), commit=True
    )

    # Seed default skills for new user
    skills = ['Python', 'English', 'JavaScript', 'Public Speaking', 'Aptitude']
    for skill in skills:
        skill_row = query_db('SELECT id FROM skills WHERE name = %s', (skill,), fetchone=True)
        if skill_row:
            query_db(
                'INSERT IGNORE INTO user_skills (user_id, skill_id, progress, xp, streak) VALUES (%s, %s, 0, 0, 0)',
                (user_id, skill_row['id']), commit=True
            )

    token = jwt.encode({
        'user_id': user_id,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')

    return jsonify({'token': token, 'name': name, 'user_id': user_id}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    user = query_db('SELECT * FROM users WHERE email = %s', (email,), fetchone=True)
    if not user or not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        return jsonify({'error': 'Invalid credentials'}), 401

    token = jwt.encode({
        'user_id': user['id'],
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')

    # Update last login
    query_db('UPDATE users SET last_login = NOW() WHERE id = %s', (user['id'],), commit=True)

    return jsonify({'token': token, 'name': user['name'], 'user_id': user['id']})

# ─── PROFILE ────────────────────────────────────────────────
@app.route('/api/profile', methods=['GET'])
@token_required
def get_profile(current_user):
    skills = query_db('''
        SELECT s.name, s.category, s.icon, us.progress, us.xp, us.streak, us.last_studied
        FROM user_skills us
        JOIN skills s ON us.skill_id = s.id
        WHERE us.user_id = %s
        ORDER BY us.xp DESC
    ''', (current_user['id'],))

    sessions_count = query_db(
        'SELECT COUNT(*) as cnt FROM chat_sessions WHERE user_id = %s',
        (current_user['id'],), fetchone=True
    )['cnt']

    total_xp = query_db(
        'SELECT COALESCE(SUM(xp), 0) as total FROM user_skills WHERE user_id = %s',
        (current_user['id'],), fetchone=True
    )['total']

    badges = query_db(
        'SELECT * FROM user_badges ub JOIN badges b ON ub.badge_id = b.id WHERE ub.user_id = %s',
        (current_user['id'],)
    )

    recent_activity = query_db('''
        SELECT cs.skill_name, cs.created_at, COUNT(cm.id) as messages
        FROM chat_sessions cs
        LEFT JOIN chat_messages cm ON cm.session_id = cs.id
        WHERE cs.user_id = %s
        GROUP BY cs.id
        ORDER BY cs.created_at DESC LIMIT 5
    ''', (current_user['id'],))

    return jsonify({
        'user': {
            'id': current_user['id'],
            'name': current_user['name'],
            'email': current_user['email'],
            'created_at': str(current_user['created_at']),
            'avatar_initials': current_user['name'][0].upper()
        },
        'skills': skills,
        'stats': {
            'sessions': sessions_count,
            'total_xp': int(total_xp),
            'skills_count': len(skills),
            'level': max(1, int(total_xp) // 500 + 1)
        },
        'badges': badges,
        'recent_activity': recent_activity
    })

# ─── SKILLS & CURRICULUM ────────────────────────────────────
@app.route('/api/skills', methods=['GET'])
@token_required
def get_skills(current_user):
    skills = query_db('SELECT * FROM skills ORDER BY category, name')
    return jsonify({'skills': skills})

@app.route('/api/skills/enroll', methods=['POST'])
@token_required
def enroll_skill(current_user):
    skill_name = request.json.get('skill_name')
    skill = query_db('SELECT * FROM skills WHERE name = %s', (skill_name,), fetchone=True)
    if not skill:
        skill_id = query_db(
            'INSERT INTO skills (name, category, icon, description) VALUES (%s, %s, %s, %s)',
            (skill_name, 'Custom', '🎯', f'Learn {skill_name} with AI guidance'),
            commit=True
        )
    else:
        skill_id = skill['id']

    query_db(
        'INSERT IGNORE INTO user_skills (user_id, skill_id, progress, xp, streak) VALUES (%s, %s, 0, 0, 0)',
        (current_user['id'], skill_id), commit=True
    )
    return jsonify({'success': True, 'message': f'Enrolled in {skill_name}'})

@app.route('/api/curriculum/<skill_name>', methods=['GET'])
@token_required
def get_curriculum(current_user, skill_name):
    curriculum = query_db(
        'SELECT * FROM curriculum WHERE skill_name = %s ORDER BY week_number, day_number',
        (skill_name,)
    )
    if not curriculum:
        curriculum = generate_curriculum_ai(skill_name)

    user_progress = query_db('''
        SELECT uc.curriculum_id, uc.completed, uc.completed_at
        FROM user_curriculum uc
        JOIN curriculum c ON uc.curriculum_id = c.id
        WHERE uc.user_id = %s AND c.skill_name = %s
    ''', (current_user['id'], skill_name))

    completed_ids = {row['curriculum_id'] for row in user_progress if row['completed']}

    result = []
    for item in curriculum:
        item['completed'] = item['id'] in completed_ids
        result.append(item)

    return jsonify({'curriculum': result, 'skill': skill_name})

def generate_curriculum_ai(skill_name):
    """Use Groq to generate a curriculum and save to DB."""
    try:
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': GROQ_MODELS[0],
                'messages': [{
                    'role': 'user',
                    'content': f'''Create a 4-week learning curriculum for "{skill_name}". 
Return ONLY a raw JSON array. Do not include markdown formatting or backticks.
Format exact example:
[{{"week_number":1,"day_number":1,"title":"Topic Title","description":"What you will learn","duration_mins":30,"type":"lesson"}}]
Include exactly 5 items per week (20 total). Types: lesson, practice, project, quiz'''
                }],
                'max_tokens': 2000
            },
            timeout=30
        )
        resp_data = response.json()
        if 'choices' not in resp_data or not resp_data['choices']:
            print(f"Curriculum AI error: {resp_data.get('error','no choices')}")
            return []
        content = resp_data['choices'][0]['message']['content'].strip()
        
        # Robust JSON cleaning
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content:
            content = content.split('```')[1].strip()
            
        items = json.loads(content)
        saved = []
        for item in items[:20]:
            item_id = query_db(
                'INSERT INTO curriculum (skill_name, week_number, day_number, title, description, duration_mins, type) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (skill_name, item.get('week_number',1), item.get('day_number',1),
                 item.get('title',''), item.get('description',''), item.get('duration_mins',30), item.get('type','lesson')),
                commit=True
            )
            item['id'] = item_id
            saved.append(item)
        return saved
    except Exception as e:
        print(f"Curriculum AI error: {e}")
        return []

@app.route('/api/curriculum/complete', methods=['POST'])
@token_required
def complete_curriculum(current_user):
    curriculum_id = request.json.get('curriculum_id')
    
    if not curriculum_id:
        return jsonify({'error': 'Curriculum ID is required'}), 400

    # 1. Fetch the curriculum item to know which skill this belongs to
    curriculum_item = query_db(
        'SELECT skill_name FROM curriculum WHERE id = %s', 
        (curriculum_id,), 
        fetchone=True
    )
    if not curriculum_item:
        return jsonify({'error': 'Curriculum item not found'}), 404
        
    skill_name = curriculum_item['skill_name']

    # 2. Check if already completed to prevent XP/Progress exploits
    already_completed = query_db(
        'SELECT completed FROM user_curriculum WHERE user_id = %s AND curriculum_id = %s',
        (current_user['id'], curriculum_id), 
        fetchone=True
    )
    if already_completed and already_completed['completed']:
        return jsonify({
            'success': True, 
            'message': 'Already completed this topic!', 
            'xp_gained': 0,
            'skill_name': skill_name
        })

    # 3. Record curriculum completion in the database
    query_db(
        '''INSERT INTO user_curriculum (user_id, curriculum_id, completed, completed_at) 
           VALUES (%s, %s, 1, NOW()) 
           ON DUPLICATE KEY UPDATE completed=1, completed_at=NOW()''',
        (current_user['id'], curriculum_id), 
        commit=True
    )
    
    # 4. Award 50 XP and 5% progress ONLY to this specific skill
    query_db(
        '''UPDATE user_skills us 
           JOIN skills s ON us.skill_id = s.id 
           SET us.xp = us.xp + 50, us.progress = LEAST(100, us.progress + 5) 
           WHERE us.user_id = %s AND s.name = %s''',
        (current_user['id'], skill_name), 
        commit=True
    )

    return jsonify({
        'success': True, 
        'message': f'Great job! Completed item in {skill_name}.',
        'xp_gained': 50, 
        'skill_name': skill_name
    })
# ─── YOUTUBE SEARCH HELPER ──────────────────────────────────
def fetch_youtube_links_internal(search_term):
    """Internal helper to get youtube links for the AI chat fallback"""
    if YOUTUBE_API_KEY and YOUTUBE_API_KEY != 'your_youtube_api_key_here':
        try:
            resp = requests.get(
                'https://www.googleapis.com/youtube/v3/search',
                params={'part': 'snippet', 'q': search_term, 'type': 'video', 'maxResults': 3, 'relevanceLanguage': 'en', 'key': YOUTUBE_API_KEY},
                timeout=5
            )
            items = resp.json().get('items', [])
            return "\n".join([f"- {v['snippet']['title']}: https://www.youtube.com/watch?v={v['id']['videoId']}" for v in items])
        except:
            pass
    
    # Fallback to standard URL
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(search_term)}"
    return f"Check out this curated YouTube search for free tutorials: {search_url}"

# ─── CHAT ────────────────────────────────────────────────────
@app.route('/api/chat/session', methods=['POST'])
@token_required
def create_session(current_user):
    skill_name = request.json.get('skill_name', 'General')
    session_id = query_db(
        'INSERT INTO chat_sessions (user_id, skill_name, created_at) VALUES (%s, %s, NOW())',
        (current_user['id'], skill_name), commit=True
    )
    return jsonify({'session_id': session_id})

@app.route('/api/chat', methods=['POST'])
@token_required
def chat_message(current_user):
    data = request.json
    user_message = data.get('message', '').strip()
    skill_name = data.get('skill_name', 'General')
    context_level = data.get('level', 'beginner')
    session_id = data.get('session_id')

    if not session_id:
        session_id = query_db(
            'INSERT INTO chat_sessions (user_id, skill_name, created_at) VALUES (%s, %s, NOW())',
            (current_user['id'], skill_name), commit=True
        )

    if not user_message:
        return jsonify({'error': 'Message empty'}), 400

    query_db(
        'INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (%s, %s, %s, NOW())',
        (session_id, 'user', user_message), commit=True
    )

    history = query_db(
        'SELECT role, content FROM chat_messages WHERE session_id = %s ORDER BY created_at DESC LIMIT 10',
        (session_id,)
    )
    history = list(reversed(history))

    # UPGRADED PROMPT: Handles diverse skills and the automatic YouTube trigger
    system_prompt = f"""You are SkillForge AI, a friendly, empathetic, and encouraging teacher helping the student learn "{skill_name}".
The student's level is: {context_level}.

Your teaching style:
- Use simple, clear language with relatable real-world analogies.
- Break complex concepts into easily digestible steps.
- Be highly encouraging and patient. Celebrate their effort! 🎉
- Ask short questions to verify they understand.
- If teaching coding: Provide clear, well-commented code blocks.
- If teaching soft skills/aptitude: Provide scenarios, frameworks, or practice questions.

CRITICAL INSTRUCTION:
If the student expresses extreme frustration, says they don't understand after multiple attempts, or explicitly asks for a video/external resource, you MUST include this exact string anywhere in your response: [YOUTUBE_TRIGGER].
Do not invent URLs. Just include [YOUTUBE_TRIGGER] and the system will attach actual links for them."""

    messages_payload = [{'role': 'system', 'content': system_prompt}]
    for msg in history[:-1]:
        messages_payload.append({'role': msg['role'], 'content': msg['content']})
    messages_payload.append({'role': 'user', 'content': user_message})

    try:
        ai_reply, err = openrouter_chat(messages_payload)
        if err:
            ai_reply = f"⚠️ {err}"
        elif '[YOUTUBE_TRIGGER]' in ai_reply:
            ai_reply = ai_reply.replace('[YOUTUBE_TRIGGER]', '').strip()
            yt_links = fetch_youtube_links_internal(f"{skill_name} {user_message} tutorial for beginners")
            ai_reply += f"\n\n📺 I can see this is a tricky topic! Sometimes watching a video helps clarify things. Here are some free resources I found for you:\n{yt_links}"
    except Exception as e:
        print(f"[Chat Exception] {type(e).__name__}: {e}")
        ai_reply = f"⚠️ Could not connect to AI service. Error: {str(e)[:100]}"

    query_db(
        'INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (%s, %s, %s, NOW())',
        (session_id, 'assistant', ai_reply), commit=True
    )

    query_db('''
        UPDATE user_skills us JOIN skills s ON us.skill_id = s.id 
        SET us.last_studied = NOW(), us.xp = us.xp + 10
        WHERE us.user_id = %s AND s.name = %s
    ''', (current_user['id'], skill_name), commit=True)

    return jsonify({'reply': ai_reply, 'session_id': session_id})



# ─── YOUTUBE SEARCH ─────────────────────────────────────────
@app.route('/api/youtube', methods=['GET'])
@token_required
def youtube_search(current_user):
    q = request.args.get('q', '').strip()
    skill = request.args.get('skill', '').strip()
    query = q or skill or 'programming tutorial'

    # Try YouTube Data API if key is set
    if YOUTUBE_API_KEY and YOUTUBE_API_KEY not in ('your_youtube_api_key_here', ''):
        try:
            resp = requests.get(
                'https://www.googleapis.com/youtube/v3/search',
                params={
                    'part': 'snippet',
                    'q': query,
                    'type': 'video',
                    'maxResults': 5,
                    'relevanceLanguage': 'en',
                    'key': YOUTUBE_API_KEY
                },
                timeout=6
            )
            data = resp.json()
            if 'items' in data and data['items']:
                videos = [{
                    'title': v['snippet']['title'],
                    'channel': v['snippet']['channelTitle'],
                    'url': f"https://www.youtube.com/watch?v={v['id']['videoId']}",
                    'thumbnail': v['snippet']['thumbnails']['medium']['url'],
                    'videoId': v['id']['videoId']
                } for v in data['items']]
                return jsonify({'videos': videos, 'source': 'api'})
        except Exception as e:
            print(f"[YouTube API] Error: {e}")

    # Fallback — return a direct YouTube search URL (no API key needed)
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query + ' tutorial')}"
    return jsonify({
        'videos': [],
        'search_url': search_url,
        'source': 'fallback'
    })

# ─── DASHBOARD ──────────────────────────────────────────────
@app.route('/api/dashboard', methods=['GET'])
@token_required
def get_dashboard(current_user):
    skills = query_db('''
        SELECT s.name, s.category, s.icon, us.progress, us.xp, us.streak, us.last_studied
        FROM user_skills us
        JOIN skills s ON us.skill_id = s.id
        WHERE us.user_id = %s
        ORDER BY us.xp DESC
    ''', (current_user['id'],))

    total_xp = query_db(
        'SELECT COALESCE(SUM(xp), 0) as total FROM user_skills WHERE user_id = %s',
        (current_user['id'],), fetchone=True
    )['total']

    sessions_count = query_db(
        'SELECT COUNT(*) as cnt FROM chat_sessions WHERE user_id = %s',
        (current_user['id'],), fetchone=True
    )['cnt']

    return jsonify({
        'user': {
            'id': current_user['id'],
            'name': current_user['name'],
            'avatar_initials': current_user['name'][0].upper()
        },
        'skills': skills,
        'stats': {
            'total_xp': int(total_xp),
            'sessions': sessions_count,
            'skills_count': len(skills),
            'level': max(1, int(total_xp) // 500 + 1)
        }
    })

# ─── PERFORMANCE ────────────────────────────────────────────
@app.route('/api/performance', methods=['GET'])
@token_required
def get_performance(current_user):
    activity = query_db('''
        SELECT DATE(created_at) as date, COUNT(*) as sessions
        FROM chat_sessions
        WHERE user_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        GROUP BY DATE(created_at)
        ORDER BY date
    ''', (current_user['id'],))

    skills = query_db('''
        SELECT s.name, s.category, us.progress, us.xp, us.streak
        FROM user_skills us
        JOIN skills s ON us.skill_id = s.id
        WHERE us.user_id = %s
        ORDER BY us.xp DESC
    ''', (current_user['id'],))

    msg_count = query_db('''
        SELECT COUNT(*) as cnt FROM chat_messages cm
        JOIN chat_sessions cs ON cm.session_id = cs.id
        WHERE cs.user_id = %s AND cm.role = 'user'
    ''', (current_user['id'],), fetchone=True)['cnt']

    return jsonify({
        'weekly_activity': activity,
        'skill_progress': skills,
        'total_messages': msg_count
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)