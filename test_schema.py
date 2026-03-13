from google.genai import types


def test():
    schema_dict = {
        "name": "book_appointment",
        "description": "Book appointment",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "name"}},
            "required": ["name"],
        },
    }

    # how to construct FunctionDeclaration from dict? We can just pass the dict if using pydantic maybe?
    # Or just construct Schema objects.

    decl = types.FunctionDeclaration(
        name=schema_dict["name"],
        description=schema_dict["description"],
        # we can pass dict directly to parameters if google-genai supports it, let's check.
    )
    print(decl)


test()
