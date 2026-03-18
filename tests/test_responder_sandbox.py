# Responder Sandbox — intentionally imperfect code for testing


def calculate_total(lst: list[int]) -> int:
    total = 0
    for i in lst:
        total = total + i
    return total


def format_output(data: list[str], verbose: bool) -> str:
    result = ""
    for item in data:
        suffix = "\n" if verbose else ", "
        result = result + str(item) + suffix
    return result
