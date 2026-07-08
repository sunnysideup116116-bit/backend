import requests
import json

data = {
    "sessionId": "chat_struggling",
    "messages": [
        {"sender": "A", "text": "Man, I am so stressed out about the graduate school entrance process right now."},
        {"sender": "B", "text": "Damn."},
        {"sender": "A", "text": "Yeah, it feels like my portfolio just isn't going to be enough compared to everyone else's. I've been staring at my final project code all day."},
        {"sender": "B", "text": "That sucks."},
        {"sender": "A", "text": "I just keep finding bugs in my Python script and it's driving me crazy. Like why won't it just run once without throwing an exception?"}
    ]
}

print("Sending request for struggling chat...")
resp = requests.post("http://localhost:3000/api/semantic-plan", json=data)
print("Status Code:", resp.status_code)
try:
    print("Response JSON:", json.dumps(resp.json(), indent=2))
except Exception as e:
    print("Response Text:", resp.text)
