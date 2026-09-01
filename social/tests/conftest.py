"""Prevent pytest collection from loading developer or production secrets."""

import os


os.environ["AYUE_SKIP_DOTENV"] = "1"
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27017"
