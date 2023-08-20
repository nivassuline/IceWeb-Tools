import apscheduler.jobstores.base
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time
import json
import random
import requests
import pytz
from datetime import datetime , timedelta
from flask import Flask, render_template, request, redirect, session,flash,jsonify,url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from pymongo import MongoClient
from google.oauth2 import credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import os
import subprocess
import tempfile
import socket

class Config:
    SCHEDULER_API_ENABLED = True
app = Flask(__name__)
app.secret_key = os.urandom(12)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
temp_csv_path = tempfile.mktemp(suffix=".csv")
socket.setdefaulttimeout(160)
CLIENT_SECRET_FILE = 'client_secret.json'  # Path to your client secret file from the Google Developers Console
SCOPES = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive.file']
db_client = MongoClient("mongodb://gsb-tracker-server:hbmQOpSniHozTWQm68LxShGOFqDLqAE5KQgvj1qGeUKne7KPhYpa9BwhhQRhkfxu6h16ffomZ9i4ACDbA5mAiA==@gsb-tracker-server.mongo.cosmos.azure.com:10255/?ssl=true&replicaSet=globaldb&retrywrites=false&maxIdleTimeMS=120000&appName=@gsb-tracker-server@")
mydb = db_client['gsb-tracker-database']
gsb_tracker_collection = mydb['data']
icewebio_collection = mydb['icewebio_data']
app.config.from_object(Config())
scheduler = BackgroundScheduler()
scheduler.start()
job_ids = []
gsb_tracker_running_jobs = []
icewebio_running_jobs = []
idle_jobs = []
instance_list = []
sheet_client = None
drive_client = None
dashboard_type = None

def get_prediction(sheet,worksheet_id,search, suffix):
    worksheet = sheet.get_worksheet_by_id(worksheet_id)
    URL = f"http://suggestqueries.google.com/complete/search?client=firefox&q={search}"
    headers = {'User-agent': 'Mozilla/5.0'}
    response = requests.get(URL, headers=headers)
    result = json.loads(response.content.decode('utf-8'))[1]
    position = 0
    for i in range(len(result)):
        print(f"position number {i + 1} = {result[i]}")
    print('-----------------------------------------')

    PST_instance = pytz.timezone('US/Pacific')
    PST = datetime.now(PST_instance)
    Time_Date = PST.strftime("%d/%m/%Y")

    for key in result:
        suffix_there = 1
        if key == f'{search} {suffix}':
            position += 1
            df = pd.DataFrame(columns=["Date", "Keyword", "Position"])
            df.loc[len(df.index)] = [Time_Date, f'{search} {suffix}', position]
            df = df.iloc[-1:]
            data_list = df.values.tolist()
            worksheet.append_row(data_list[0],value_input_option='USER_ENTERED')
            break
        else:
            position += 1
            suffix_there = 0
    if suffix_there == 0:
        df = pd.DataFrame(columns=["Date", "Keyword", "Position"])
        df.loc[len(df.index)] = [Time_Date, f'{search} {suffix}', 0]
        df = df.iloc[-1:]
        data_list = df.values.tolist()
        worksheet.append_row(data_list[0],value_input_option='USER_ENTERED')

def create_drive_folder(driver_service,audiences_name):
    parent_drive_id = '0AGV9xa1MUaL9Uk9PVA'

    # Define folder metadata.
    folder_metadata = {
        'name': audiences_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_drive_id]
    }



    # Create the folder.
    created_folder = driver_service.files().create(body=folder_metadata,supportsAllDrives=True,fields='id').execute()

    return created_folder.get('id')


def icewebio(driver_serivce,local_csv_path,drive_id,company_name,company_id):
    source_path = f"s3://org-672-1tijkxkhoj1gcbxcioiw4mbhziokhuse1a-s3alias/org-672/audience-{company_id}/"
    # Run the AWS S3 ls command and capture the output
    aws_s3_command = ["aws", "s3", "ls", source_path]
    output = subprocess.run(aws_s3_command, capture_output=True, text=True, check=True).stdout

    # Process the output to find the latest file
    files_list = output.strip().split('\n')
    if files_list:
        latest_file_info = files_list[-1].split()
        if len(latest_file_info) == 4:  # Make sure it's a valid ls output
            latest_file_name = latest_file_info[-1]
    else:
        print("No files found in the specified S3 path.")
        
    # Construct the aws s3 cp command to download the file
    aws_s3_download_command = ["aws", "s3", "cp", f'{source_path}{latest_file_name}', local_csv_path]
    # Run the command
    try:
        subprocess.run(aws_s3_download_command, check=True)
        print("File downloaded successfully!")
    except subprocess.CalledProcessError as e:
        print(f"Error downloading file: {e}")

    # Read the downloaded CSV file using pandas
    df = pd.read_csv(local_csv_path)

    # Remove duplicate rows based on the "email" column
    df_unique = df.drop_duplicates(subset=["email"])

    # Convert the date column to datetime type
    df_unique["date"] = pd.to_datetime(df_unique["date"])

    # Get yesterday's date
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    # Filter rows to include only data from yesterday
    df_filtered = df_unique[df_unique["date"].dt.date == yesterday.date()]

    # Define the desired column order
    desired_columns_order = ['date',"firstName","lastName","facebook","linkedIn","twitter","email","optIn","optInDate","optInIp","optInUrl","pixelFirstHitDate","pixelLastHitDate","bebacks","phone","dnc","age","gender","maritalStatus","address","city","state","zip","householdIncome","netWorth","incomeLevels","peopleInHousehold","adultsInHousehold","childrenInHousehold","veteransInHousehold","education","creditRange","ethnicGroup","generation","homeOwner","occupationDetail","politicalParty","religion","childrenBetweenAges0_3","childrenBetweenAges4_6","childrenBetweenAges7_9","childrenBetweenAges10_12","childrenBetweenAges13_18","behaviors","childrenAgeRanges","interests","ownsAmexCard","ownsBankCard","dwellingType","homeHeatType","homePrice","homePurchasedYearsAgo","homeValue","householdNetWorth","language","mortgageAge","mortgageAmount","mortgageLoanType","mortgageRefinanceAge","mortgageRefinanceAmount","mortgageRefinanceType","isMultilingual","newCreditOfferedHousehold","numberOfVehiclesInHousehold","ownsInvestment","ownsPremiumAmexCard","ownsPremiumCard","ownsStocksAndBonds","personality","isPoliticalContributor","isVoter","premiumIncomeHousehold","urbanicity","maid","maidOs"]  # Specify the columns in your desired order

    # Rearrange columns in the desired order
    df_filtered = df_filtered[desired_columns_order]

    rows_count = df_filtered['date'].count()

    df_filtered.to_csv(local_csv_path, index=False)

    output_csv_filename = f"{yesterday_str}_{rows_count}_{company_name}_icewebio.csv"


    # Define file metadata and upload settings.
    file_metadata = {
        'name': output_csv_filename,
        'parents': [drive_id]  

        }
    media = MediaFileUpload(local_csv_path, mimetype='text/csv')

    uploaded_file = driver_serivce.files().create(
        body=file_metadata, media_body=media, supportsAllDrives=True,fields='id').execute()

    print(f'File uploaded: {uploaded_file.get("id")}')


def delete_drive_folder(driver_service,drive_id):
    driver_service.files().delete(fileId=drive_id).execute()


def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def create_client():
    if 'credentials' not in session:
        return None
    creds = credentials.Credentials.from_authorized_user_info(session['credentials'], SCOPES)
    gspread_client = gspread.authorize(creds)
    drive_client = build('drive', 'v3', credentials=creds)
    return gspread_client , drive_client

@app.route('/')
def index():
    if 'credentials' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('solutions_dashboard'))


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
    return redirect(url_for('solutions_dashboard'))

@app.route('/logout')
def logout():
    if 'credentials' in session:
        del session['credentials']
    return 'Logged out successfully!'


@app.route("/solutions-dashboard")
def solutions_dashboard():
    global sheet_client
    global drive_client
    sheet_client , drive_client = create_client()
    if sheet_client is None or drive_client is None:
        return redirect(url_for('login'))
    return render_template('solutions_dashboard.html')

@app.route("/icewebio-dashboard")
def icewebio_dashboard():
    global dashboard_type
    global drive_client
    if drive_client is None:
        return redirect(url_for('login'))
    dashboard_type = 'icewebio'
    data = icewebio_collection.find()
    instance_list.clear()
    idle_jobs.clear()
    for d in data:
        instance_list.append(d['company_name'])
    for instance_name in instance_list:
        if instance_name not in icewebio_running_jobs:
            idle_jobs.append(instance_name)
    print(idle_jobs)
    print(instance_list)
    print(icewebio_running_jobs)
    return render_template('icewebio_dashboard.html', instance_list=instance_list, running_jobs=icewebio_running_jobs,idle_jobs=idle_jobs)


@app.route("/gsb-tracker-dashboard")
def gsb_tracker_dashboard():
    global dashboard_type
    global sheet_client
    if sheet_client is None:
        return redirect(url_for('login'))
    dashboard_type = 'tracker'
    data = gsb_tracker_collection.find()
    instance_list.clear()
    idle_jobs.clear()
    for d in data:
        instance_list.append(d['name'])
    for instance_name in instance_list:
        if instance_name not in gsb_tracker_running_jobs:
            idle_jobs.append(instance_name)
    print(idle_jobs)
    print(instance_list)
    print(gsb_tracker_running_jobs)
    return render_template('gsb_tracker_dashboard.html', instance_list=instance_list, running_jobs=gsb_tracker_running_jobs,idle_jobs=idle_jobs)

@app.route("/add", methods=['POST'])
def add():
    if dashboard_type == 'tracker':
        sheet = sheet_client.open_by_url(
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
            gsb_tracker_collection.insert_one(data)
            response = {'message': 'Company added successfully'}
        else:
            response = {'message': 'Company already in database'}
        return response
    if dashboard_type == 'icewebio':
        company_name = request.form['name']
        if company_name not in instance_list:
            drive_id = create_drive_folder(drive_client,company_name)
            data = {
                "company_name" : request.form['name'],
                "company_id" : request.form['id'],
                "drive_folder_id" : drive_id,
                "drive_folder_url" : f"https://drive.google.com/drive/folders/{drive_id}",
                "was_started" : "0"

            }
            icewebio_collection.insert_one(data)
            response = {'message': 'Company added successfully'}
        else:
            response = {'message': 'Company already in database'}


@app.route("/jobs/<name>", methods=['GET'])
def names(name):
    if dashboard_type == 'tracker':
        instance = gsb_tracker_collection.find_one({'name': name})
        instance_name = instance['name']
        instance_search = instance['search']
        instance_suffix = instance['suffix']
        instance_id = instance['worksheet_id']
        instance_url = instance['worksheet_url']
        return render_template(
            'gsb_tracker_instance_details.html',
            instance_name=instance_name,
            instance_search=instance_search,
            instance_suffix=instance_suffix,
            instance_id=instance_id,
            instance_url=instance_url
            )
    if dashboard_type == 'icewebio':
        instance = icewebio_collection.find_one({'company_name': name})
        instance_name = instance['company_name']
        instance_id = instance['company_id']
        folder_id = instance['drive_folder_id']
        folder_url = instance['drive_folder_url']
        return render_template(
            'icewebio_instance_details.html',
            instance_name=instance_name,
            instance_id=instance_id,
            folder_id=folder_id,
            folder_url=folder_url
            )



@app.route("/run/<instance_name>", methods=['POST'])
def run(instance_name):
    if dashboard_type == 'tracker':
        sheet = sheet_client.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        try:
            instance = gsb_tracker_collection.find_one({'name': instance_name})
            instance_name = instance['name']
            instance_search = instance['search']
            instance_suffix = instance['suffix']
            instance_id = instance['worksheet_id']
            if instance['was_started'] == "0":
                instance['was_started'] = '1'
                get_prediction(sheet,instance_id,instance_search,instance_suffix)
                gsb_tracker_collection.update_one({"_id": instance["_id"]}, {"$set": instance})

            scheduler.add_job(id=instance_name, func=get_prediction, trigger="interval", seconds=86400,
                            args=[sheet, instance_id, instance_search, instance_suffix])
            gsb_tracker_running_jobs.append(instance_name)
            idle_jobs.remove(instance_name)
        except apscheduler.jobstores.base.ConflictingIdError:
            print('Job already running')
        
        return redirect('/gsb-tracker-dashboard')
    if dashboard_type == 'icewebio':
        try:
            instance = icewebio_collection.find_one({'company_name': instance_name})
            instance_name = instance['company_name']
            instance_id = instance['company_id']
            folder_id = instance['drive_folder_id']
            trigger = OrTrigger([CronTrigger(hour=12, minute=0)])
            scheduler.add_job(id=instance_name, func=icewebio, trigger=trigger,
                            args=[drive_client,temp_csv_path,folder_id,instance_name,instance_id])
            icewebio_running_jobs.append(instance_name)
            idle_jobs.remove(instance_name)
        except apscheduler.jobstores.base.ConflictingIdError:
            print('Job already running')
        
        return redirect('/icewebio-dashboard')


@app.route("/runnow")
def runnow():
    for instance_name in idle_jobs:
        try:
            instance = icewebio_collection.find_one({'company_name': instance_name})
            instance_name = instance['company_name']
            instance_id = instance['company_id']
            folder_id = instance['drive_folder_id']
            scheduler.add_job(id=instance_name, func=icewebio, trigger="interval", seconds=60,
                            args=[drive_client,temp_csv_path,folder_id,instance_name,instance_id])
            icewebio_running_jobs.append(instance_name)
            idle_jobs.remove(instance_name)
        except apscheduler.jobstores.base.ConflictingIdError:
            print('Job already running')

@app.route("/runall")
def runall():
    if dashboard_type == 'tracker':
        sheet = sheet_client.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        for instance_name in idle_jobs:
            try:
                instance = gsb_tracker_collection.find_one({'name': instance_name})
                instance_name = instance['name']
                instance_search = instance['search']
                instance_suffix = instance['suffix']
                instance_id = instance['worksheet_id']
                if instance['was_started'] == "0":
                    instance['was_started'] = '1'
                    get_prediction(sheet,instance_id,instance_search,instance_suffix)
                    gsb_tracker_collection.update_one({"_id": instance["_id"]}, {"$set": instance})

                scheduler.add_job(id=instance_name, func=get_prediction, trigger="interval", seconds=86400,
                                args=[sheet, instance_id, instance_search, instance_suffix])
                gsb_tracker_running_jobs.append(instance_name)
                idle_jobs.remove(instance_name)
            except apscheduler.jobstores.base.ConflictingIdError:
                print('Job already running')
        
        return redirect('/gsb-tracker-dashboard')

    if dashboard_type == 'icewebio':
        for instance_name in idle_jobs:
            try:
                instance = icewebio_collection.find_one({'company_name': instance_name})
                instance_name = instance['company_name']
                instance_id = instance['company_id']
                folder_id = instance['drive_folder_id']
                trigger = OrTrigger([CronTrigger(hour=14, minute=0)])
                scheduler.add_job(id=instance_name, func=icewebio, trigger=trigger,
                                args=[drive_client,temp_csv_path,folder_id,instance_name,instance_id])
                icewebio_running_jobs.append(instance_name)
                idle_jobs.remove(instance_name)
            except apscheduler.jobstores.base.ConflictingIdError:
                print('Job already running')
        
        return redirect('/icewebio-dashboard')

@app.route("/stop/<instance_name>", methods=['POST'])
def stop(instance_name):
    if dashboard_type == 'tracker':
        try:
            scheduler.remove_job(instance_name)
            gsb_tracker_running_jobs.remove(instance_name)
        except (apscheduler.jobstores.base.JobLookupError,ValueError):
            print("Job Is Idle")
        
        return redirect('/gsb-tracker-dashboard')
    if dashboard_type == 'icewebio':
        try:
            scheduler.remove_job(instance_name)
            icewebio_running_jobs.remove(instance_name)
        except (apscheduler.jobstores.base.JobLookupError,ValueError):
            print("Job Is Idle")
        
        return redirect('/icewebio-dashboard')

@app.route("/stopall")
def stopall():
    if dashboard_type == 'tracker':
        for instance_name in gsb_tracker_running_jobs:
            try:
                scheduler.remove_job(instance_name)
                gsb_tracker_running_jobs.remove(instance_name)
            except (apscheduler.jobstores.base.JobLookupError,ValueError):
                print("Job Is Idle")
            
            return redirect('/gsb-tracker-dashboard')
    if dashboard_type == 'icewebio':
        for instance_name in icewebio_running_jobs:
            try:
                scheduler.remove_job(instance_name)
                icewebio_running_jobs.remove(instance_name)
            except (apscheduler.jobstores.base.JobLookupError,ValueError):
                print("Job Is Idle")
            
            return redirect('/icewebio-dashboard')

@app.route("/delete/<instance_name>", methods=['POST'])
def delete(instance_name):
    if dashboard_type == 'tracker':
        sheet = sheet_client.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        try:
            instance = gsb_tracker_collection.find_one({'name': instance_name})
            gsb_tracker_collection.delete_one({'name': instance_name})
            sheet.del_worksheet_by_id(instance['worksheet_id'])
            if instance_name in gsb_tracker_running_jobs:
                gsb_tracker_running_jobs.remove(instance_name)
            elif instance_name in idle_jobs:
                idle_jobs.remove(instance_name)
        except TypeError:
            pass
        return redirect('/gsb-tracker-dashboard')
    if dashboard_type == 'icewebio':
        try:
            instance = icewebio_collection.find_one({'company_name': instance_name})
            icewebio_collection.delete_one({'company_name': instance_name})
            drive_client.files().delete(fileId=instance['drive_folder_id'],supportsAllDrives=True).execute()
            if instance_name in icewebio_running_jobs:
                icewebio_running_jobs.remove(instance_name)
            elif instance_name in idle_jobs:
                idle_jobs.remove(instance_name)
        except TypeError:
            pass
        return redirect('/icewebio-dashboard')
