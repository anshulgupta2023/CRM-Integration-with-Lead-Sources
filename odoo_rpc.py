# Import the libraries we need
import os
import json
import requests
import dotenv

# Load values like URL, username, password, API key from the .env file
dotenv.load_dotenv()

# Read .env values and store them in variables
URL = os.getenv("ODOO_URL")        # Example: http://localhost:8069
DB  = os.getenv("ODOO_DB")         # Example: odoo18
USER= os.getenv("ODOO_USER")       # Example: admin
PWD = os.getenv("ODOO_PWD")        # Example: admin

# Create a place to store UID globally so we use it again later
_uid = None

# Function to send a request to Odoo (like mailing a letter)
def _rpc(payload):
    # This sends a POST request to Odoo with your JSON data
    r = requests.post(f"{URL}/jsonrpc", json=payload, timeout=60)
    r.raise_for_status()               # If Odoo doesn't respond, stop

    resp = r.json()                    # Convert reply to a dictionary
    if "error" in resp:               # If Odoo replied with an error
        print(" Odoo Error:")
        print(json.dumps(resp["error"], indent=2))   # Print the error nicely
        raise Exception("Odoo returned an error")    # Stop the script
    if "result" not in resp:
        return None
    return resp["result"]             # Otherwise return the success result

# Login function (gets your user ID by using your username & password)
def login():
    global _uid                       # So we can reuse uid later
    if _uid is None:
        _uid = _rpc({
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "login",
                "args": [DB, USER, PWD]   # This logs in with credentials
            },
            "id": 1
        })
    return _uid

# This function sends a request to do something in Odoo (like create a lead)
def call(model, method, *args):
    uid = login()     # Get the logged-in user ID
    return _rpc({
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                DB,         #  database name
                uid,        # The user ID returned from login()
                PWD,        # The same password used during login!
                model,      # Like 'crm.lead'
                method,     # Like 'create'
                *args       # Your data (e.g., payloads)
            ]
        },
        "id": 2
    })

