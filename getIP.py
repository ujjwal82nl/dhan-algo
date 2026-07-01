
import http.client
import json  # Import the built-in json module

conn = http.client.HTTPSConnection("api.dhan.co")

headers = {
    'access-token': "eyJ0eXAiOiRTMNGiLCJklihtOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZabcdfexNzgyMNUYNzQzLCJpYXQiOjE3ODI1MDQzNDMsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTAxNzUyMjc3In0.SmA2x6xpIGL5kCd5BLbzrak_KGg-bh9AoCeWPjma4wStGsYhF-pQ9POW8MSCRWEwc8Og1NVnTQtGpOVre1234g",
    'Accept': "application/json"
}

conn.request("GET", "/v2/ip/getIP", headers=headers)

res = conn.getresponse()
data = res.read()

# 1. Decode the raw bytes into a string
json_string = data.decode("utf-8")

# 2. Parse the string into a native Python dictionary
parsed_json = json.loads(json_string)

# 3. Pretty print it with an indentation of 4 spaces
print(json.dumps(parsed_json, indent=4))

