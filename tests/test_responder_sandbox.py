# Responder Sandbox — intentionally imperfect code for testing


def calculate_total(lst: list[int]) -> int:
    x = 0
    for i in lst:
        x = x + i
    return x


def format_output(data: list[str], verbose: bool) -> str:
    result = ""
    for item in data:
        suffix = "\n" if verbose else ", "
        result = result + str(item) + suffix
    return result
