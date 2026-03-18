# Responder Sandbox — intentionally imperfect code for testing

def calculate_total(lst):
    x = 0
    for i in lst:
        x = x + i
    return x

def format_output(data, verbose):
    result = ""
    for item in data:
        if verbose == True:
            result = result + str(item) + "\n"
        else:
            result = result + str(item) + ", "
    return result

def fetch_data(url, timeout):
    import requests
    resp = requests.get(url, timeout=timeout)
    data = resp.json()
    return data
