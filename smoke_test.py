from multi_db_query_engine.client import MultiDBQueryEngine

try:
    with MultiDBQueryEngine() as engine:
        print("Available databases:", engine.databases)

        response = engine.ask(
            "Show all orders with customer names and amounts",
            database="local",
        )

        print("\nQuery response:")
        print(response.model_dump_json(indent=2))

        if response.status == "completed" and response.data:
            print("\nEND-TO-END TEST: PASSED")
        else:
            print("\nEND-TO-END TEST: CHECK RESPONSE")
finally:
    pass