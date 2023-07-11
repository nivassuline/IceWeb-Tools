import apscheduler.jobstores.base
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time
import json
import requests
import pytz
from datetime import datetime
from flask import Flask, render_template, request, redirect, session,flash,jsonify,url_for
from apscheduler.schedulers.background import BackgroundScheduler
from pymongo import MongoClient
from google.oauth2 import credentials
from google_auth_oauthlib.flow import Flow
import os



class Config:
    SCHEDULER_API_ENABLED = True
app = Flask(__name__)
app.secret_key = os.urandom(12)
CLIENT_SECRET_FILE = 'client_secret.json'  # Path to your client secret file from the Google Developers Console
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
db_client = MongoClient("mongodb://gsb-tracker-server:x8TGD9v7xINfI8ncBYDbWIliECcPfHdOwiebTfHcJAULYnggF6pJTpSWZjoGvg2HcsvAOK6TwPJ4ACDb41VGtw==@gsb-tracker-server.mongo.cosmos.azure.com:10255/gsb-tracker-database?ssl=true&replicaSet=globaldb&retrywrites=false&maxIdleTimeMS=120000&appName=@gsb-tracker-server@")
mydb = db_client['main']
collection = mydb['data']
app.config.from_object(Config())
scheduler = BackgroundScheduler()
scheduler.start()
job_ids = []
running_jobs = []
idle_jobs = []
instance_list = []
client = None

def get_prediction(sheet,worksheet_id,search, suffix):
    worksheet = sheet.get_worksheet_by_id(worksheet_id)
    URL = f"http://suggestqueries.google.com/complete/search?client=firefox&q={search}%20"
    headers = {'User-agent': 'Mozilla/5.0'}
    response = requests.get(URL, headers=headers)
    result = json.loads(response.content.decode('utf-8'))[1]
    position = 0
    for i in range(len(result)):
        print(f"position number {i + 1} = {result[i]}")
    print('-----------------------------------------')

    PST_instance = pytz.timezone('US/Pacific')
    PST = datetime.now(PST_instance)
    Time_Date = PST.strftime("%d/%m/%Y %H:%M:%S")

    for key in result:
        suffix_there = 1
        if key == f'{search} {suffix}':
            position += 1
            df = pd.DataFrame(columns=["Time and Date", "Keyword", "Position"])
            df.loc[len(df.index)] = [Time_Date, f'{search} {suffix}', position]
            df = df.iloc[-1:]
            data_list = df.values.tolist()
            worksheet.append_row(data_list[0])
            break
        else:
            position += 1
            suffix_there = 0
    if suffix_there == 0:
        df = pd.DataFrame(columns=["Time and Date", "Keyword", "Position"])
        df.loc[len(df.index)] = [Time_Date, f'{search} {suffix}', 0]
        df = df.iloc[-1:]
        data_list = df.values.tolist()
        worksheet.append_row(data_list[0])

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def create_gspread_client():
    if 'credentials' not in session:
        return None
    creds = credentials.Credentials.from_authorized_user_info(session['credentials'], SCOPES)
    client = gspread.authorize(creds)
    return client

@app.route('/')
def index():
    if 'credentials' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('dashboard'))


@app.route('/login-page')
def login_page():
    return render_template('login.html')
@app.route('/login')
def login():
    flow = Flow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for('authorize', _external=True)
    authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/authorize')
def authorize():
    state = session['state']
    flow = Flow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = url_for('authorize', _external=True)
    authorization_response = request.url
    flow.fetch_token(authorization_response=authorization_response)
    credentials = flow.credentials
    session['credentials'] = credentials_to_dict(credentials)
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    if 'credentials' in session:
        del session['credentials']
    return 'Logged out successfully!'


@app.route("/dashboard")
def dashboard():
    global client
    client = create_gspread_client()
    if client is None:
        return redirect(url_for('login'))
    data = collection.find()
    instance_list.clear()
    idle_jobs.clear()
    for d in data:
        instance_list.append(d['name'])
    for instance_name in instance_list:
        if instance_name not in running_jobs:
            idle_jobs.append(instance_name)
    print(idle_jobs)
    print(instance_list)
    print(running_jobs)
    return render_template('index.html', instance_list=instance_list, running_jobs=running_jobs,idle_jobs=idle_jobs)



@app.route("/add", methods=['POST'])
def add():
    sheet = client.open_by_url(
    'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
    instance_name = request.form['name']
    if instance_name not in instance_list:
        worksheet_list = [title.title for title in sheet.worksheets()]
        if instance_name not in worksheet_list:
            worksheet = sheet.add_worksheet(title=instance_name, rows="100", cols="20")
            df = pd.DataFrame(columns=["Time and Date", "Keyword", "Position"])
            columns_name = list(df.columns)
            worksheet.append_row(columns_name)
        elif instance_name in worksheet_list:
            index = worksheet_list.index(instance_name)
            worksheet = sheet.get_worksheet(index=index)
        data = {
            "name": request.form['name'],
            "search": request.form['search'],
            "suffix": request.form['suffix'],
            "worksheet_id": worksheet.id,
            "worksheet_url": worksheet.url,
            "was_started": "0"
        }
        collection.insert_one(data)
        response = {'message': 'Company added successfully'}


    else:
        response = {'message': 'Company already in database'}
    return response




@app.route("/jobs/<name>", methods=['GET'])
def names(name):
    instance = collection.find_one({'name': name})
    instance_name = instance['name']
    instance_search = instance['search']
    instance_suffix = instance['suffix']
    instance_id = instance['worksheet_id']
    instance_url = instance['worksheet_url']
    return render_template(
        'instance_details.html',
        instance_name=instance_name,
        instance_search=instance_search,
        instance_suffix=instance_suffix,
        instance_id=instance_id,
        instance_url=instance_url
        )


@app.route("/run/<instance_name>", methods=['POST'])
def run(instance_name):
    sheet = client.open_by_url(
    'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
    try:
        instance = collection.find_one({'name': instance_name})
        instance_name = instance['name']
        instance_search = instance['search']
        instance_suffix = instance['suffix']
        instance_id = instance['worksheet_id']
        if instance['was_started'] == "0":
            instance['was_started'] = '1'
            get_prediction(sheet,instance_id,instance_search,instance_suffix)
            collection.update_one({"_id": instance["_id"]}, {"$set": instance})

        scheduler.add_job(id=instance_name, func=get_prediction, trigger="interval", seconds=86400,
                        args=[sheet, instance_id, instance_search, instance_suffix])
        running_jobs.append(instance_name)
        idle_jobs.remove(instance_name)
    except apscheduler.jobstores.base.ConflictingIdError:
        print('Job already running')
    
    return redirect('/dashboard')

@app.route("/stop/<instance_name>", methods=['POST'])
def stop(instance_name):
    try:
        scheduler.remove_job(instance_name)
        running_jobs.remove(instance_name)
        return redirect('/')
    except apscheduler.jobstores.base.JobLookupError:
        print("Job Is Idle")
        return redirect('/dashboard')

@app.route("/delete/<instance_name>", methods=['POST'])
def delete(instance_name):
    sheet = client.open_by_url(
    'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
    try:
        instance = collection.find_one({'name': instance_name})
        collection.delete_one({'name': instance_name})
        sheet.del_worksheet_by_id(instance['worksheet_id'])
        if instance_name in running_jobs:
            running_jobs.remove(instance_name)
        elif instance_name in idle_jobs:
            idle_jobs.remove(instance_name)
    except TypeError:
        pass
    return redirect('/dashboard')
