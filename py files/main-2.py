# iterative name inputs
## and the final print
names = []

def capitalize_name(name: str):
    return name.capitalize()


def add_names(first_name: str, last_name: str):
    return first_name + ' ' + last_name

def get_names():
    for i in range(2): # loops
        first_name = input("what's your first name?")
        last_name = input("what's your last name?")
        first_name = capitalize_name(first_name)
        last_name = capitalize_name(last_name)

        if first_name == capitalize_name('ali'): # if control statement
            first_name = 'MR. ' + first_name

        final_name = add_names(first_name, last_name)

        names.append(final_name)

for name in names:
    print(f"Your final name is {name}")