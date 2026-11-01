first_name = input("what's your first name?")
last_name = input("what's your last name?")

def capitalize_name(name: str):
    return name.capitalize()


def add_names(first_name: str, last_name: str):
    return first_name + ' ' + last_name

first_name = capitalize_name(first_name)
last_name = capitalize_name(last_name)

final_name = add_names(first_name, last_name)

print(f"Your final name is {final_name}")