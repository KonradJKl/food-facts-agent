def test_package_is_importable() -> None:
    import food_facts_agent

    assert food_facts_agent.__name__ == "food_facts_agent"
