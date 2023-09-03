import apscheduler.jobstores.base
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from serpapi import GoogleSearch
import pytz
import socket
import json
from urllib.parse import urlparse
from datetime import datetime , timedelta
from flask import Flask, render_template, request, redirect, session,url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from pymongo import MongoClient
from google.oauth2 import credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os
import subprocess
import tempfile
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
import logging
import pytz
from datetime import datetime
import pandas as pd


class Config:
    SCHEDULER_API_ENABLED = True
app = Flask(__name__)
app.secret_key = os.urandom(12)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
app.config.from_object(Config())
scheduler = BackgroundScheduler()
logging.getLogger('apscheduler').setLevel(logging.DEBUG)
socket.setdefaulttimeout(600)
scheduler.start()
logging.basicConfig()
CLIENT_SECRET_FILE = 'client_secrets.json'  # Path to your client secret file from the Google Developers Console
SCOPES = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive.file']
TRIGGER = OrTrigger([CronTrigger(hour=14, minute=0)])
DB_CONNECTOR = MongoClient("mongodb://gsb-tracker-server:hbmQOpSniHozTWQm68LxShGOFqDLqAE5KQgvj1qGeUKne7KPhYpa9BwhhQRhkfxu6h16ffomZ9i4ACDbA5mAiA==@gsb-tracker-server.mongo.cosmos.azure.com:10255/?ssl=true&replicaSet=globaldb&retrywrites=false&maxIdleTimeMS=120000&appName=@gsb-tracker-server@")
DB_CLIENT = DB_CONNECTOR['gsb-tracker-database']
GSB_TRACKER_COLLECTION = DB_CLIENT['data']
PEOPLEDATA_COLLECTION = DB_CLIENT['icewebio_data']
S3_ORG_COLLECTION = DB_CLIENT['s3_buckets']
GSB_TRACKER_RUNNING_JOBS = []
PEOPLEDATA_RUNNING_JOBS = []
IDLE_JOBS = []
INSTANCE_LIST = []
ORG_LIST = []
SHEET_CLIENT = None
DRIVE_CLIENT = None
GAUTH_CLIENT = None
OLD_DRIVE_CLIENT = None
DASHBOARD_TYPE = None

def get_prediction(sheet, worksheet_id, search, suffix):
    final_search = f'{search} {suffix}'
    params = {
        "engine": "google_autocomplete",
        "q": search,
        "api_key": "d6eb01cffb495aa0456cda70138efdc71d1722dc7881efed01a992bd29cd564a"
    }
    
    worksheet = sheet.get_worksheet_by_id(worksheet_id)
    search_obj = GoogleSearch(params)
    result = search_obj.get_dict()["suggestions"]
    
    PST_instance = pytz.timezone('US/Pacific')
    PST = datetime.now(PST_instance)
    Time_Date = PST.strftime("%d/%m/%Y")
    
    position = 0
    suffix_there = 0  # Initialize suffix_there outside the loop
    
    for key in result:
        position += 1
        if key['value'].lower() == final_search.lower():
            suffix_there = 1
            break
    
    df = pd.DataFrame(columns=["Date", "Keyword", "Position"])
    df.loc[0] = [Time_Date, f'{search} {suffix}', position if suffix_there == 1 else 0]
    
    data_list = df.values.tolist()
    worksheet.append_row(data_list[0], value_input_option='USER_ENTERED')


def icewebio(drive_client,gauth,drive_id,bucket_string,audience_name,audience_id,rules):
    if gauth.access_token_expired:
        # Refresh them if expired
        gauth.Refresh()
        drive_client = GoogleDrive(gauth)
    
    local_csv_path = tempfile.mktemp(suffix=".csv")
    source_path = f"{bucket_string}audience-{audience_id}/"
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

    
    # Initialize an empty list to store the indices of rows to delete
    rows_to_delete = []

    for id in df["id"].unique():
        name_rows = df[df["id"] == id]
        df_paths = name_rows["url"].apply(lambda x: x.replace(f'{urlparse(x).scheme}://{urlparse(x).netloc}', ''))

        # Initialize a boolean flag to check if any rule matches
        rule_matches = False

        for rule_name, rule_data in rules.items():
            exclude_type = rule_data["type"]
            exclude_url = rule_data["url"]

            if exclude_type == "Exact Match":
                rule_matches |= any(df_paths == exclude_url)
            elif exclude_type == "Contains":
                rule_matches |= any(df_paths.str.contains(exclude_url))
            elif exclude_type == "Starts With":
                rule_matches |= any(df_paths.str.startswith(exclude_url))
            elif exclude_type == "Ends With":
                rule_matches |= any(df_paths.str.endswith(exclude_url))

        # If any rule matches, mark the rows for deletion
        if rule_matches:
            rows_to_delete.extend(name_rows.index)

    # Extract emails to delete
    emails_to_delete = df.loc[rows_to_delete, "ip"]

    # Filter the DataFrame to exclude rows with matching emails
    filtered_data = df[~df["ip"].isin(emails_to_delete)]


    # Remove duplicate rows based on the "email" column
    df_unique = filtered_data.drop_duplicates(subset=["ip"])

    # Convert the date column to datetime type
    df_unique["date"] = pd.to_datetime(df_unique["date"])

    # Get yesterday's date
    yesterday = datetime.now() - timedelta(days=17)
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    # Filter rows to include only data from yesterday
    df_filtered = df_unique[df_unique["date"].dt.date == yesterday.date()]

    # Define the desired column order
    desired_columns_order = ['date',"url","firstName","lastName","facebook","linkedIn","twitter","email","optIn","optInDate","optInIp","optInUrl","pixelFirstHitDate","pixelLastHitDate","bebacks","phone","dnc","age","gender","maritalStatus","address","city","state","zip","householdIncome","netWorth","incomeLevels","peopleInHousehold","adultsInHousehold","childrenInHousehold","veteransInHousehold","education","creditRange","ethnicGroup","generation","homeOwner","occupationDetail","politicalParty","religion","childrenBetweenAges0_3","childrenBetweenAges4_6","childrenBetweenAges7_9","childrenBetweenAges10_12","childrenBetweenAges13_18","behaviors","childrenAgeRanges","interests","ownsAmexCard","ownsBankCard","dwellingType","homeHeatType","homePrice","homePurchasedYearsAgo","homeValue","householdNetWorth","language","mortgageAge","mortgageAmount","mortgageLoanType","mortgageRefinanceAge","mortgageRefinanceAmount","mortgageRefinanceType","isMultilingual","newCreditOfferedHousehold","numberOfVehiclesInHousehold","ownsInvestment","ownsPremiumAmexCard","ownsPremiumCard","ownsStocksAndBonds","personality","isPoliticalContributor","isVoter","premiumIncomeHousehold","urbanicity","maid","maidOs"]  

    # # Rearrange columns in the desired order
    df_filtered = df_filtered[desired_columns_order]
    rows_count = df_filtered['date'].count()

    output_csv_filename = f"{yesterday_str}_{rows_count}_{audience_name}_icewebio.csv"

    # Create a new file in the specified folder
    gfile = drive_client.CreateFile({
        'title': output_csv_filename,
        'mimeType': 'text/csv',
        'parents': [{'kind': 'drive#driveItem', 'id': drive_id}]
    })

    gfile.SetContentFile(local_csv_path)

    gfile.Upload(param={'supportsTeamDrives': True})

    print(f'File uploaded: {output_csv_filename}')



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

def delete_drive_folder(driver_service,drive_id):
    driver_service.files().delete(fileId=drive_id).execute(num_retries=5)


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
    gauth = GoogleAuth(settings_file='settings.yaml')
    gauth.LoadCredentialsFile('credentials.json')
    if gauth.access_token_expired:
        # Refresh them if expired
        gauth.Refresh()
    else:
        # Initialize the saved creds
        gauth.Authorize()
    # Save the current credentials to a file
    gauth.SaveCredentialsFile("credentials.json")
    drive_client = GoogleDrive(gauth)
    old_drive_client = build('drive', 'v3', credentials=creds)
    return gspread_client , drive_client, old_drive_client, gauth




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
    global SHEET_CLIENT
    global DRIVE_CLIENT
    global OLD_DRIVE_CLIENT
    global GAUTH_CLIENT
    SHEET_CLIENT , DRIVE_CLIENT, OLD_DRIVE_CLIENT , GAUTH_CLIENT = create_client()
    if SHEET_CLIENT is None or DRIVE_CLIENT is None or OLD_DRIVE_CLIENT is None:
        return redirect(url_for('login'))
    return render_template('solutions_dashboard.html')

@app.route("/icewebio-dashboard")
def icewebio_dashboard():
    global DASHBOARD_TYPE
    global DRIVE_CLIENT
    if DRIVE_CLIENT is None:
        return redirect(url_for('login'))
    DASHBOARD_TYPE = 'icewebio'
    data = PEOPLEDATA_COLLECTION.find()
    INSTANCE_LIST.clear()
    IDLE_JOBS.clear()
    ORG_LIST.clear()
    for d in data:
        INSTANCE_LIST.append(d['aud_name'])
    for org in S3_ORG_COLLECTION.find():
        ORG_LIST.append(org['org_name'])
    for instance_name in INSTANCE_LIST:
        if instance_name not in PEOPLEDATA_RUNNING_JOBS:
            IDLE_JOBS.append(instance_name)
    print(IDLE_JOBS)
    print(INSTANCE_LIST)
    print(PEOPLEDATA_RUNNING_JOBS)
    return render_template('icewebio_dashboard.html',
                            instance_list=INSTANCE_LIST,
                            running_jobs=PEOPLEDATA_RUNNING_JOBS,
                            idle_jobs=IDLE_JOBS,
                            org_list=ORG_LIST)

@app.route("/icewebio-dashboard/add-org")
def add_bucket():
    return render_template('icewebio_add_org.html')

@app.route("/gsb-tracker-dashboard")
def gsb_tracker_dashboard():
    global DASHBOARD_TYPE
    global SHEET_CLIENT
    if SHEET_CLIENT is None:
        return redirect(url_for('login'))
    DASHBOARD_TYPE = 'tracker'
    data = GSB_TRACKER_COLLECTION.find()
    INSTANCE_LIST.clear()
    IDLE_JOBS.clear()
    for d in data:
        INSTANCE_LIST.append(d['name'])
    for instance_name in INSTANCE_LIST:
        if instance_name not in GSB_TRACKER_RUNNING_JOBS:
            IDLE_JOBS.append(instance_name)
    print(IDLE_JOBS)
    print(INSTANCE_LIST)
    print(GSB_TRACKER_RUNNING_JOBS)
    return render_template('gsb_tracker_dashboard.html', instance_list=INSTANCE_LIST, running_jobs=GSB_TRACKER_RUNNING_JOBS,idle_jobs=IDLE_JOBS)

@app.route("/add", methods=['POST'])
def add():
    if DASHBOARD_TYPE == 'tracker':
        sheet = SHEET_CLIENT.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        instance_name = request.form['name']
        if instance_name not in INSTANCE_LIST:
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
            GSB_TRACKER_COLLECTION.insert_one(data)
            response = {'message': 'Company added successfully'}
        else:
            response = {'message': 'Company already in database'}
        return response
    if DASHBOARD_TYPE == 'icewebio':
        aud_name = request.form['aud_name']
        if aud_name not in INSTANCE_LIST:
            drive_id = create_drive_folder(OLD_DRIVE_CLIENT,aud_name)
            data = {
                "aud_name" : request.form['aud_name'],
                "aud_id" : request.form['aud_id'],
                "org_name" : request.form['orgs'],
                "drive_folder_id" : drive_id,
                "drive_folder_url" : f"https://drive.google.com/drive/folders/{drive_id}",
                "exclude_urls" : []
            }
            PEOPLEDATA_COLLECTION.insert_one(data)
            response = {'message': 'Company added successfully'}
        else:
            response = {'message': 'Company already in database'}


@app.route("/icewebio-dashboard/add-org-to-db", methods=['POST'])
def add_bucket_to_db():
    org = request.form['org'].lower()
    bucket = request.form['bucket'].lower()
    if not S3_ORG_COLLECTION.find_one({'org_name': org}):
        data = {
            "org_name" : org,
            "bucket" : bucket
        }
        S3_ORG_COLLECTION.insert_one(data)
    return redirect('/icewebio-dashboard/add-org')

@app.route("/icewebio-dashboard/add_exclude_urls_to_db/<instance_name>", methods=['POST'])
def add_exclude_urls_to_db(instance_name):
    instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': instance_name})
    data = {}
    exclude_types = request.form.getlist('dynamic_exclude_type[]')
    exclude_urls = request.form.getlist('dynamic_exclude_url[]')
    for filter_type, url,index in zip(exclude_types, exclude_urls,range(len(exclude_types))):
        data[f'rule{index}'] = {
            'type': filter_type,
            'url': url
        }
    print(data)
    instance["rules"] = data
    PEOPLEDATA_COLLECTION.update_one({"_id": instance["_id"]}, {"$set": instance})
    return redirect(f'/jobs/{instance_name}')





@app.route("/jobs/<name>", methods=['GET'])
def names(name):
    if DASHBOARD_TYPE == 'tracker':
        instance = GSB_TRACKER_COLLECTION.find_one({'name': name})
        instance_aud_name = instance['name']
        instance_search = instance['search']
        instance_suffix = instance['suffix']
        instance_aud_id = instance['worksheet_id']
        instance_url = instance['worksheet_url']
        return render_template(
            'gsb_tracker_instance_details.html',
            instance_name=instance_aud_name,
            instance_search=instance_search,
            instance_suffix=instance_suffix,
            instance_id=instance_aud_id,
            instance_url=instance_url
            )
    if DASHBOARD_TYPE == 'icewebio':
        instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': name})
        instance_aud_name = instance['aud_name']
        instance_aud_id = instance['aud_id']
        instance_org_name = instance['org_name']
        folder_id = instance['drive_folder_id']
        folder_url = instance['drive_folder_url']
        instance_rules = instance['rules']
        return render_template(
            'icewebio_instance_details.html',
            instance_aud_name=instance_aud_name,
            instance_aud_id=instance_aud_id,
            instance_org_name = instance_org_name,
            folder_id=folder_id,
            folder_url=folder_url,
            instance_rules=instance_rules
            )



@app.route("/run/<instance_name>", methods=['POST'])
def run(instance_name):
    if DASHBOARD_TYPE == 'tracker':
        sheet = SHEET_CLIENT.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        try:
            instance = GSB_TRACKER_COLLECTION.find_one({'name': instance_name})
            instance_name = instance['name']
            instance_search = instance['search']
            instance_suffix = instance['suffix']
            instance_id = instance['worksheet_id']
            if instance['was_started'] == "0":
                instance['was_started'] = '1'
                get_prediction(sheet,instance_id,instance_search,instance_suffix)
                GSB_TRACKER_COLLECTION.update_one({"_id": instance["_id"]}, {"$set": instance})
            scheduler.add_job(id=instance_name, func=get_prediction, trigger=TRIGGER,misfire_grace_time=15*60,
                            args=[sheet, instance_id, instance_search, instance_suffix])
            GSB_TRACKER_RUNNING_JOBS.append(instance_name)
            IDLE_JOBS.remove(instance_name)
        except apscheduler.jobstores.base.ConflictingIdError:
            print('Job already running')
        
        return redirect('/gsb-tracker-dashboard')
    if DASHBOARD_TYPE == 'icewebio':
        try:
            instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': instance_name})
            instance_aud_name = instance['aud_name']
            instance_aud_id = instance['aud_id']
            instance_org_name = instance['org_name']
            folder_id = instance['drive_folder_id']
            exclude_url_list = instance['exclude_urls']
            for document in S3_ORG_COLLECTION.find():
                if instance_org_name == document['org_name']:
                    bucket_string = document['bucket']
            scheduler.add_job(id=instance_name, func=icewebio, trigger=TRIGGER,misfire_grace_time=15*60,
                            args=[DRIVE_CLIENT,GAUTH_CLIENT,folder_id,bucket_string,instance_aud_name,instance_aud_id,exclude_url_list])
            PEOPLEDATA_RUNNING_JOBS.append(instance_name)
            IDLE_JOBS.remove(instance_name)
        except apscheduler.jobstores.base.ConflictingIdError:
            print('Job already running')
        
        return redirect('/icewebio-dashboard')


@app.route("/runnow/<instance_name>")
def runnow(instance_name):
    try:
        instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': instance_name})
        instance_aud_name = instance['aud_name']
        instance_aud_id = instance['aud_id']
        instance_org_name = instance['org_name']
        folder_id = instance['drive_folder_id']
        exclude_url_list = instance['exclude_urls']
        for document in S3_ORG_COLLECTION.find():
            if instance_org_name == document['org_name']:
                bucket_string = document['bucket']
        scheduler.add_job(id=instance_name, func=icewebio,
                        args=[DRIVE_CLIENT,GAUTH_CLIENT,folder_id,bucket_string,instance_aud_name,instance_aud_id,exclude_url_list])
    except apscheduler.jobstores.base.ConflictingIdError:
        print('Job already running')
    
    return redirect('/icewebio-dashboard')


@app.route("/runall")
def runall():
    if DASHBOARD_TYPE == 'tracker':
        sheet = SHEET_CLIENT.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        for instance_name in IDLE_JOBS:
            try:
                instance = GSB_TRACKER_COLLECTION.find_one({'name': instance_name})
                instance_name = instance['name']
                instance_search = instance['search']
                instance_suffix = instance['suffix']
                instance_id = instance['worksheet_id']
                if instance['was_started'] == "0":
                    instance['was_started'] = '1'
                    get_prediction(sheet,instance_id,instance_search,instance_suffix)
                    GSB_TRACKER_COLLECTION.update_one({"_id": instance["_id"]}, {"$set": instance})
                scheduler.add_job(id=instance_name, func=get_prediction, trigger=TRIGGER,misfire_grace_time=15*60,
                                args=[sheet, instance_id, instance_search, instance_suffix])
                GSB_TRACKER_RUNNING_JOBS.append(instance_name)
                IDLE_JOBS.remove(instance_name)
            except apscheduler.jobstores.base.ConflictingIdError:
                print('Job already running')
        
        return redirect('/gsb-tracker-dashboard')

    if DASHBOARD_TYPE == 'icewebio':
        for instance_name in IDLE_JOBS:
            try:
                instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': instance_name})
                instance_aud_name = instance['aud_name']
                instance_aud_id = instance['aud_id']
                instance_org_name = instance['org_name']
                folder_id = instance['drive_folder_id']
                exclude_url_list = instance['exclude_urls']
                scheduler.add_job(id=instance_name, func=icewebio, trigger=TRIGGER,misfire_grace_time=15*60,
                                args=[DRIVE_CLIENT,GAUTH_CLIENT,folder_id,instance_org_name,instance_aud_name,instance_aud_id,exclude_url_list])
                PEOPLEDATA_RUNNING_JOBS.append(instance_name)
                IDLE_JOBS.remove(instance_name)
            except apscheduler.jobstores.base.ConflictingIdError:
                print('Job already running')
        
        return redirect('/icewebio-dashboard')

@app.route("/stop/<instance_name>", methods=['POST'])
def stop(instance_name):
    if DASHBOARD_TYPE == 'tracker':
        try:
            scheduler.remove_job(instance_name)
            GSB_TRACKER_RUNNING_JOBS.remove(instance_name)
        except (apscheduler.jobstores.base.JobLookupError,ValueError):
            print("Job Is Idle")
        
        return redirect('/gsb-tracker-dashboard')
    if DASHBOARD_TYPE == 'icewebio':
        try:
            scheduler.remove_job(instance_name)
            PEOPLEDATA_RUNNING_JOBS.remove(instance_name)
        except (apscheduler.jobstores.base.JobLookupError,ValueError):
            print("Job Is Idle")
        
        return redirect('/icewebio-dashboard')

@app.route("/stopall")
def stopall():
    if DASHBOARD_TYPE == 'tracker':
        for instance_name in GSB_TRACKER_RUNNING_JOBS:
            try:
                scheduler.remove_job(instance_name)
                GSB_TRACKER_RUNNING_JOBS.remove(instance_name)
            except (apscheduler.jobstores.base.JobLookupError,ValueError):
                print("Job Is Idle")
            
            return redirect('/gsb-tracker-dashboard')
    if DASHBOARD_TYPE == 'icewebio':
        for instance_name in PEOPLEDATA_RUNNING_JOBS:
            try:
                scheduler.remove_job(instance_name)
                PEOPLEDATA_RUNNING_JOBS.remove(instance_name)
            except (apscheduler.jobstores.base.JobLookupError,ValueError):
                print("Job Is Idle")
            
            return redirect('/icewebio-dashboard')

@app.route("/delete/<instance_name>", methods=['POST'])
def delete(instance_name):
    if DASHBOARD_TYPE == 'tracker':
        sheet = SHEET_CLIENT.open_by_url(
        'https://docs.google.com/spreadsheets/d/1BrWUSdQd2ztr0bCcePhFCU4mbG1AwZHzfcWup3njvb8/edit#gid=0')
        try:
            instance = GSB_TRACKER_COLLECTION.find_one({'name': instance_name})
            GSB_TRACKER_COLLECTION.delete_one({'name': instance_name})
            sheet.del_worksheet_by_id(instance['worksheet_id'])
            if instance_name in GSB_TRACKER_RUNNING_JOBS:
                GSB_TRACKER_RUNNING_JOBS.remove(instance_name)
            elif instance_name in IDLE_JOBS:
                IDLE_JOBS.remove(instance_name)
        except TypeError:
            pass
        return redirect('/gsb-tracker-dashboard')
    if DASHBOARD_TYPE == 'icewebio':
        try:
            instance = PEOPLEDATA_COLLECTION.find_one({'aud_name': instance_name})
            PEOPLEDATA_COLLECTION.delete_one({'aud_name': instance_name})
            OLD_DRIVE_CLIENT.files().delete(fileId=instance['drive_folder_id'],supportsAllDrives=True).execute()
            if instance_name in PEOPLEDATA_RUNNING_JOBS:
                PEOPLEDATA_RUNNING_JOBS.remove(instance_name)
            elif instance_name in IDLE_JOBS:
                IDLE_JOBS.remove(instance_name)
        except TypeError:
            pass
        return redirect('/icewebio-dashboard')
    
