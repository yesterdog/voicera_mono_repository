import os

DEFAULT_CONFIG = {
    "server_ip": os.environ.get("SOFTPHONE_SERVER_IP", "129.121.140.95"),
    "server_port": int(os.environ.get("SOFTPHONE_SERVER_PORT", "5062")),
    "domain": os.environ.get("SOFTPHONE_DOMAIN", "voicera-jambonz.local"),
    "username": os.environ.get("SOFTPHONE_USERNAME", "softphone"),
    "password": os.environ.get("SOFTPHONE_PASSWORD", "softphone"),
}
