#!/usr/bin/env python3

import os
from dotenv import load_dotenv

from src.actors.coordinator_actor import CoordinatorActor
from src.llm.openai_client import OpenAIChatLLM
from src.message_bus import MessageBus

def test_budget_manager():
    load_dotenv()

    bus = MessageBus()

    def llm_factory(name: str):
        # LLM will read config from system.yml automatically
        return OpenAIChatLLM()

    coordinator = CoordinatorActor(llm=llm_factory("Coordinator"), llm_factory=llm_factory)
    bus.register(coordinator)

    # Test 1: Create BudgetManager with $50
    print("Test 1: Creating BudgetManager with $50")
    response1 = bus.send(from_actor="User", to_actor="Coordinator", message="Create a budget and call is BudgetManager, set it to $50")
    print(f"Response: {response1}")

    # Test 2: Increase budget to $58
    print("\nTest 2: Increasing budget to $58")
    response2 = bus.send(from_actor="User", to_actor="Coordinator", message="Tell the BudgetManager to set the budget to $58")
    print(f"Response: {response2}")
    state = bus.actors["BudgetManager"].state
    assert "budget" in state, "BudgetManager should have a 'budget' key in state"
    assert state["budget"] == 58, "BudgetManager should have a 'budget' of 58"
        
    # Test 3: Query current budget
    print("\nTest 3: Querying current budget")
    response3 = bus.send(from_actor="User", to_actor="Coordinator", message="What is the current budget?")
    print(f"Response: {response3}")

    # Test 4: Increase budget by $8
    print("\nTest 4: Increasing budget by $8")
    response4 = bus.send(from_actor="User", to_actor="Coordinator", message="Tell the BudgetManager to increase the budget by $8")
    print(f"Response: {response4}")
    state = bus.actors["BudgetManager"].state
    assert "budget" in state, "BudgetManager should have a 'budget' key in state"
    assert state["budget"] == 66, "BudgetManager should have a 'budget' of 66"

    # Test 5: Query current budget again
    print("\nTest 5: Querying current budget again")
    response5 = bus.send(from_actor="User", to_actor="Coordinator", message="What is the current budget?")
    print(f"Response: {response5}")

    # Check state of BudgetManager if exists
    if "BudgetManager" in bus.actors:
        actor = bus.actors["BudgetManager"]
        print(f"\nBudgetManager state: {actor.state}")
        # Flexible assertions for BudgetManager state - LLM manages the state structure
        # Accept any of these budget key names
        budget_key = None
        for key in ["current_budget", "budget", "total_budget", "budget_total"]:
            if key in actor.state:
                budget_key = key
                break
        assert budget_key is not None, f"No budget field found in state: {actor.state.keys()}"
        assert isinstance(actor.state[budget_key], (int, float))  # Budget is numeric
        # Verify the budget value is correct (should be 66 after increases)
        assert actor.state[budget_key] == 66  # 58 + 8 = 66

    # Flexible response content assertions - check for budget-related content
    
    assert "budget" in response3.lower()  # Query response mentions budget
    assert "budget" in response5.lower()  # Final query response mentions budget

if __name__ == "__main__":
    test_budget_manager()