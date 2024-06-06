import requests
import logging
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from pymongo import MongoClient
import bcrypt
import os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import urllib.parse  
import shazamio
import asyncio
from bson.objectid import ObjectId
from pydub import AudioSegment
from googleapiclient.discovery import build

app = Flask(__name__)
app.secret_key = 'mysecret'

YOUTUBE_API_KEY = 'AIzaSyD8Nhvx6a0WjjtArrjjp2fk-P110LC8gJI'
logging.basicConfig(level=logging.DEBUG)

# MongoDB configuration
client = MongoClient('localhost', 27017)  # Connect to MongoDB
db = client['myDatabase']

youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

shazam = shazamio.Shazam()

UPLOAD_FOLDER = 'uploads'  # 음악 업로드
ALLOWED_EXTENSIONS = {'wav', 'mp3'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# 프로필 사진 업로드 경로 설정
UPLOAD_PROFILE_FOLDER = os.path.join('static', 'uploads_profile')
PROFILE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_PROFILE_FOLDER'] = UPLOAD_PROFILE_FOLDER

# 폴더가 없으면 생성
if not os.path.exists(UPLOAD_PROFILE_FOLDER):
    os.makedirs(UPLOAD_PROFILE_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def profile_allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in PROFILE_ALLOWED_EXTENSIONS

def convert_to_wav(source_file):
    sound = AudioSegment.from_file(source_file)
    wav_filename = os.path.splitext(os.path.basename(source_file))[0] + '.wav'
    wav_path = os.path.join(app.config['UPLOAD_FOLDER'], wav_filename)
    sound.export(wav_path, format="wav")
    return wav_path

def get_user_playlists(username):
    playlists = db.playlists.find({'username': username})
    playlist_data = []
    for playlist in playlists:
        playlist_data.append({
            'playlist_name': playlist['playlist_name'],
            '_id': playlist['_id']
        })
    return playlist_data

def get_uploaded_files():
    files = os.listdir(app.config['UPLOAD_FOLDER'])
    return [f for f in files if allowed_file(f)]

def get_youtube_video_url(query):
    search_url = "https://www.googleapis.com/youtube/v3/search"
    search_params = {
        'part': 'snippet',
        'q': query,
        'key': YOUTUBE_API_KEY,
        'type': 'video',
        'maxResults': 1
    }
    response = requests.get(search_url, params=search_params)
    data = response.json()
    logging.debug(f"Response from YouTube API for query '{query}': {data}")
    if 'items' in data and len(data['items']) > 0:
        video_id = data['items'][0]['id']['videoId']
        return f"https://www.youtube.com/watch?v={video_id}"
    return None

# 초기화 시 좋아요 필드를 추가하는 함수
def initialize_likes():
    db.playlists.update_many({}, {'$set': {'likes': 0}})

# 서버 시작 시 좋아요 필드를 추가
initialize_likes()

@app.route('/')
def home():
    # 추천 플레이리스트 가져오기 (좋아요 순으로 상위 3개, 좋아요 수가 같으면 이름순)
    recommended_playlists = list(db.playlists.find().sort([("likes", -1), ("playlist_name", 1)]).limit(3))
    for playlist in recommended_playlists:
        playlist['songs'] = list(db.songs.find({'playlist_id': playlist['_id']}))

    # 인기 사용자 가져오기 (총 좋아요 수가 많은 상위 3명, 좋아요 수가 같으면 이름순)
    users_likes = db.playlists.aggregate([
        {"$group": {"_id": "$username", "total_likes": {"$sum": "$likes"}}},
        {"$sort": {"total_likes": -1, "_id": 1}},
        {"$limit": 3}
    ])

    popular_users = []
    for user in users_likes:
        user_playlists = list(db.playlists.find({"username": user["_id"]}))
        for playlist in user_playlists:
            playlist['songs'] = list(db.songs.find({'playlist_id': playlist['_id']}))
        popular_users.append({
            "_id": user["_id"],
            "total_likes": user["total_likes"],
            "playlists": user_playlists
        })

    return render_template('home.html', recommended_playlists=recommended_playlists, popular_users=popular_users)

@app.route('/search_song_page')
def search_song_page():
    return render_template('search.html')

@app.route('/search_song', methods=['POST'])
def search_song():
    song_title = request.form.get('song_title')
    if song_title:
        song_info = asyncio.run(search_song_info(song_title))
        if song_info:
            # Get user's playlists for the dropdown
            if 'username' in session:
                user_playlists = get_user_playlists(session['username'])
                return render_template('search.html', song_info=song_info, playlists=user_playlists)
            else:
                flash('로그인이 필요합니다.')
                return redirect(url_for('login'))
        else:
            flash('No song information found.')
    else:
        flash('Song title not provided.')
    return redirect(url_for('search_song_page'))

async def search_song_info(song_title):
    try:
        search_result = await shazam.search_track(song_title)
        if search_result['tracks']['hits']:
            track = search_result['tracks']['hits'][0]['track']
            song_info = {
                'title': track['title'],
                'artist': track['subtitle'],
                'description': track['share']['text']
            }
            return song_info
        else:
            logging.debug(f"No tracks found for: {song_title}")
            return None
    except Exception as e:
        logging.error(f"Error occurred: {e}")
        return None

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        users = db.users
        existing_user = users.find_one({'username': request.form['username']})

        if existing_user is None:
            hashpass = bcrypt.hashpw(request.form['password'].encode('utf-8'), bcrypt.gensalt())
            email = request.form['email']

            profile_picture = request.files['profile_picture']
            profile_picture_filename = None

            if profile_picture and profile_allowed_file(profile_picture.filename):
                filename = secure_filename(profile_picture.filename)
                profile_picture_filename = f"{request.form['username']}_{filename}"
                profile_picture.save(os.path.join(app.config['UPLOAD_PROFILE_FOLDER'], profile_picture_filename))

            user_data = {
                'username': request.form['username'],
                'password': hashpass,
                'email': email,
                'profile_picture': profile_picture_filename,
                'notifications_enabled': False  # 기본값 설정
            }

            users.insert_one(user_data)
            session['username'] = request.form['username']
            session['profile_picture'] = profile_picture_filename
            flash('가입되었습니다')
            return redirect(url_for('home'))
        
        flash('이미 존재하는 사용자 이름입니다!')
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        users = db.users
        login_user = users.find_one({'username': request.form['username']})

        if login_user:
            if bcrypt.checkpw(request.form['password'].encode('utf-8'), login_user['password']):
                session['username'] = request.form['username']
                session['profile_picture'] = login_user.get('profile_picture', None)
                flash('로그인되었습니다')
                return redirect(url_for('home'))
            else:
                flash('잘못된 비밀번호입니다')
        else:
            flash('잘못된 사용자 이름입니다')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/profile')
def profile():
    if 'username' in session:
        user = db.users.find_one({'username': session['username']})
        if user:
            user['created_at'] = user.get('created_at', 'N/A')  # 추가된 부분
        return render_template('profile.html', user=user)
    return redirect(url_for('login'))

@app.route('/create_playlist', methods=['POST'])
def create_playlist():
    if 'username' in session:
        data = request.json if request.is_json else request.form
        playlist_name = data.get('playlist_name')
        existing_playlist = db.playlists.find_one({'username': session['username'], 'playlist_name': playlist_name})
        if existing_playlist is None:
            db.playlists.insert_one({'username': session['username'], 'playlist_name': playlist_name, 'likes': 0})
            flash('플레이리스트가 생성되었습니다.')
        else:
            flash('플레이리스트 이름이 이미 존재합니다.')
        return redirect(url_for('my_playlists'))
    flash('로그인이 필요합니다.')
    return redirect(url_for('login'))

@app.route('/delete_playlist', methods=['POST'])
def delete_playlist():
    if 'username' in session:
        try:
            playlist_id = request.form['playlist_id']
            logging.debug(f"Attempting to delete playlist with ID: {playlist_id}")
            if playlist_id:  # Check if playlist_id is not empty
                result = db.playlists.delete_one({'_id': ObjectId(playlist_id), 'username': session['username']})
                if result.deleted_count == 1:
                    db.songs.delete_many({'playlist_id': ObjectId(playlist_id)})
                    flash('플레이리스트가 삭제되었습니다.')
                else:
                    flash('플레이리스트 삭제에 실패하였습니다.')
            else:
                flash('플레이리스트 ID가 제공되지 않았습니다.')
        except Exception as e:
            logging.error(f"Error deleting playlist: {e}")
            flash('플레이리스트 삭제 중 오류가 발생했습니다.')
        return redirect(url_for('my_playlists'))
    flash('로그인이 필요합니다.')
    return redirect(url_for('login'))

@app.route('/my_playlists', methods=['GET', 'POST'])
def my_playlists():
    if 'username' in session:
        playlists = list(db.playlists.find({'username': session['username']}))

        for playlist in playlists:
            playlist['songs'] = list(db.songs.find({'playlist_id': playlist['_id']}))
        
        return render_template('my_playlists.html', playlists=playlists)
    return redirect(url_for('login'))

@app.route('/like_playlist', methods=['POST'])
def like_playlist():
    if 'username' in session:
        playlist_id = request.form['playlist_id']
        try:
            user_id = session['username']
            
            # 사용자가 이미 해당 플레이리스트에 좋아요를 눌렀는지 확인
            already_liked = db.likes.find_one({'username': user_id, 'playlist_id': playlist_id})
            if already_liked:
                # 좋아요 취소
                result = db.playlists.update_one({'_id': ObjectId(playlist_id)}, {'$inc': {'likes': -1}})
                if result.modified_count == 1:
                    db.likes.delete_one({'username': user_id, 'playlist_id': playlist_id})
                    flash('플레이리스트에 대한 좋아요가 취소되었습니다.')
                else:
                    flash('플레이리스트 좋아요 취소에 실패했습니다.')
            else:
                # 좋아요 추가
                playlist = db.playlists.find_one({'_id': ObjectId(playlist_id)})
                if not playlist:
                    flash('플레이리스트를 찾을 수 없습니다.')
                    return redirect(url_for('popular_playlists'))

                result = db.playlists.update_one({'_id': ObjectId(playlist_id)}, {'$inc': {'likes': 1}})
                if result.modified_count == 1:
                    db.likes.insert_one({'username': user_id, 'playlist_id': playlist_id})
                    flash('플레이리스트에 좋아요를 눌렀습니다.')
                else:
                    flash('플레이리스트 좋아요에 실패했습니다.')
        except Exception as e:
            logging.error(f"Error liking/unliking playlist: {e}")
            flash('플레이리스트 좋아요/취소 중 오류가 발생했습니다.')
        return redirect(url_for('popular_playlists'))
    flash('로그인이 필요합니다.')
    return redirect(url_for('login'))


@app.route('/popular_playlists')
def popular_playlists():
    playlists = list(db.playlists.find().sort([("likes", -1), ("playlist_name", 1)]))
    for playlist in playlists:
        playlist['songs'] = list(db.songs.find({'playlist_id': playlist['_id']}))
    return render_template('popular_playlists.html', playlists=playlists)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'username' in session:
        user = db.users.find_one({'username': session['username']})

        if request.method == 'POST':
            if 'delete_account' in request.form:
                if 'profile_picture' in user and user['profile_picture']:
                    profile_picture_path = os.path.join(app.config['UPLOAD_PROFILE_FOLDER'], user['profile_picture'])
                    if os.path.exists(profile_picture_path):
                        os.remove(profile_picture_path)
                db.users.delete_one({'username': session['username']})
                session.pop('username', None)
                flash('계정이 삭제되었습니다.')
                return redirect(url_for('login'))
            
            new_username = request.form['username']
            new_email = request.form['email']
            current_password = request.form['current_password']
            new_password = request.form['new_password']
            notifications_enabled = 'notifications_enabled' in request.form

            if user and bcrypt.checkpw(current_password.encode('utf-8'), user['password']):
                updates = {}
                if new_username:
                    updates['username'] = new_username
                if new_email:
                    updates['email'] = new_email
                if new_password:
                    updates['password'] = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
                updates['notifications_enabled'] = notifications_enabled

                if 'profile_picture' in request.files:
                    profile_picture = request.files['profile_picture']
                    if profile_picture and profile_allowed_file(profile_picture.filename):
                        extension = profile_picture.filename.rsplit('.', 1)[1].lower()
                        filename = f"{session['username']}.{extension}"
                        profile_picture_path = os.path.join(app.config['UPLOAD_PROFILE_FOLDER'], filename)
                        profile_picture.save(profile_picture_path)
                        updates['profile_picture'] = filename
                        session['profile_picture'] = filename  # Save profile picture to session
                        print(f"Saved profile picture to session: {filename}")  # Log added

                if updates:
                    db.users.update_one({'username': session['username']}, {'$set': updates})
                    if 'username' in updates:
                        session['username'] = updates['username']
                    flash('프로필이 업데이트되었습니다.')
            else:
                flash('현재 비밀번호가 일치하지 않습니다.')

        return render_template('settings.html', user=user)
    return redirect(url_for('login'))

@app.route('/add_track', methods=['POST'])
def add_track():
    if 'username' in session:
        username = request.json.get('username')
        video_id = request.json.get('videoId')
        track = {'videoId': video_id}
        db.playlists.insert_one({'username': username, 'track': track})
        return jsonify({'success': True})
    return jsonify({'success': False}), 403

@app.route('/ai_features', methods=['GET', 'POST'])
def ai_features():
    song_info = None
    playlists = []
    uploaded_files = []
    if 'username' in session:
        playlists = get_user_playlists(session['username'])
    if request.method == 'POST':
        if 'audio' in request.files:
            file = request.files['audio']
            if file.filename == '':
                flash('선택된 파일이 없습니다')
                return redirect(request.url)
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                
                if file_path.endswith('.mp3'):
                    file_path = convert_to_wav(file_path)
                
                try:
                    song_info = asyncio.run(process_audio(file_path))
                except Exception as e:
                    flash(f'오디오 처리 중 오류 발생: {e}')
                    return redirect(request.url)
            else:
                flash('지원되지 않는 파일 형식입니다. WAV 및 MP3 파일만 허용됩니다.')
                return redirect(request.url)
        elif 'existing_file' in request.form:
            existing_file = request.form['existing_file']
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], existing_file)
            try:
                song_info = asyncio.run(process_audio(file_path))
            except Exception as e:
                flash(f'오디오 처리 중 오류 발생: {e}')
                return redirect(request.url)
    uploaded_files = get_uploaded_files()
    return render_template('ai_features.html', song_info=song_info, playlists=playlists, uploaded_files=uploaded_files)

@app.route('/process_existing_file', methods=['POST'])
def process_existing_file():
    if 'username' in session:
        existing_file = request.form['existing_file']
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], existing_file)
        try:
            song_info = asyncio.run(process_audio(file_path))
            playlists = get_user_playlists(session['username'])
            uploaded_files = get_uploaded_files()
            return render_template('ai_features.html', song_info=song_info, playlists=playlists, uploaded_files=uploaded_files)
        except Exception as e:
            flash(f'오디오 처리 중 오류 발생: {e}')
            return redirect(url_for('ai_features'))
    flash('로그인이 필요합니다.')
    return redirect(url_for('login'))

@app.route('/save_to_playlist', methods=['POST'])
def save_to_playlist():
    if 'username' in session:
        track = request.form['track']
        artist = request.form['artist']
        playlist_name = request.form.get('playlist')
        new_playlist_name = request.form.get('new_playlist')

        if playlist_name:
            playlist = db.playlists.find_one({'username': session['username'], 'playlist_name': playlist_name})
        else:
            playlist = None

        if playlist is None and new_playlist_name:
            # Create a new playlist if it does not exist
            playlist_id = db.playlists.insert_one({'username': session['username'], 'playlist_name': new_playlist_name}).inserted_id
            playlist = db.playlists.find_one({'_id': playlist_id})
        
        if playlist:
            song_entry = {
                'playlist_id': playlist['_id'],
                'track': track,
                'artist': artist,
                'username': session['username']
            }
            db.songs.insert_one(song_entry)
            flash('노래가 플레이리스트에 저장되었습니다.')
        else:
            flash('플레이리스트를 찾을 수 없습니다.')
        return redirect(url_for('ai_features'))
    flash('로그인이 필요합니다.')
    return redirect(url_for('login'))

@app.route('/play_playlist', methods=['POST'])
def play_playlist():
    playlist_id = request.json['playlist_id']
    songs = list(db.songs.find({'playlist_id': ObjectId(playlist_id)}))
    video_urls = []

    for song in songs:
        query = f"{song['track']} {song['artist']}"
        request = youtube.search().list(q=query, part='snippet', type='video', maxResults=1)
        response = request.execute()
        if response['items']:
            video_id = response['items'][0]['id']['videoId']
            video_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1"
            video_urls.append(video_url)

    return jsonify(video_urls=video_urls)

async def process_audio(audio_path):
    out = await shazam.recognize(audio_path)
    song_info = {
        'track': out['track']['title'],
        'artist': out['track']['subtitle']
    }
    return song_info

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)
