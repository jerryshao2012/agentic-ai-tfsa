# tfsa_assistant_test.py
import sys
import os

# Add the parent directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import logging

from tfsa_assistant_graph import run_tfsa_assistant_sync

# ======================
# 6. Example Usage
# ======================
if __name__ == "__main__":
    logging.info("===== TFSA CONTRIBUTION ASSISTANT =====")

    # Example 1: Policy question
    logging.info(
        "\n=== EXAMPLE 1: Policy Question ===\nWhat are the annual dollar limits for each year of TSFA, including 2025?")
    response_text, _ = run_tfsa_assistant_sync("What are the annual dollar limits for each year of TSFA, including 2025?")
    logging.info(response_text)
    # Sample answer:
    # I believe you're asking about TFSA (Tax-Free Savings Account) contribution limits. Here are the annual TFSA contribution room limits for each year since the program began in Canada:
    # TFSA Annual Contribution Limits:
    #
    # 2009-2012: $5,000 per year
    # 2013-2014: $5,500 per year
    # 2015: $10,000 (temporary increase)
    # 2016-2018: $5,500 per year
    # 2019-2022: $6,000 per year
    # 2023: $6,500
    # 2024: $7,000
    # 2025: $7,000
    #
    # Total cumulative contribution room for someone who was eligible since 2009 and has never contributed would be $95,000 as of 2025.
    # Important notes:
    #
    # These limits are indexed to inflation and rounded to the nearest $500
    # Unused contribution room carries forward indefinitely
    # You regain contribution room in January following any withdrawals made in the previous year
    # You must be 18 or older and a Canadian resident to contribute
    #
    # The limits are set annually by the Canada Revenue Agency based on inflation adjustments to the original $5,000 base amount.

    # Example 2: Contribution intent
    logging.info("\n=== EXAMPLE 2: Contribution Intent ===\nI want to contribute to my TFSA")
    response_text, _ = run_tfsa_assistant_sync("I want to contribute to my TFSA")
    logging.info(response_text)
    # Sample answer:
    # I'd be happy to help you with information about TFSA contributions! To provide the most relevant guidance, could you tell me a bit more about your situation?
    # For example:
    #
    # Do you already have a TFSA account set up, or would you need to open one first?
    # Are you looking to make a one-time contribution or set up regular contributions?
    # Do you know how much contribution room you currently have available?
    #
    # In the meantime, here are some key things to keep in mind:
    # Before contributing:
    #
    # Make sure you don't exceed your available contribution room (this includes any unused room from previous years plus this year's limit)
    # You can check your contribution room on your CRA My Account online, or call the CRA
    #
    # Ways to contribute:
    #
    # Online banking transfer to your TFSA
    # In-person at your bank or financial institution
    # Pre-authorized contributions (automatic transfers)
    # By cheque or bank draft
    #
    # Investment options within TFSA:
    #
    # High-interest savings accounts
    # GICs (Guaranteed Investment Certificates)
    # Mutual funds, ETFs, stocks (if your TFSA allows investments)
    #
    # What specific aspect of contributing would you like to focus on?

    # Example 3: Contribution room check (with user ID)
    logging.info(
        "\n=== EXAMPLE 3: Contribution Room Check ===\nMy user ID is user_123. What is my contribution room for 2025?")
    response_text, state = run_tfsa_assistant_sync("My user ID is user_123. What is my contribution room for 2025?")
    # Display response
    logging.info(response_text)
    # Sample answer:
    # Based on the information I found, your current TFSA contribution room for 2025 is $14,500.
    # This means you can contribute up to $14,500 to your TFSA this year without exceeding your limit. This amount includes:
    #
    # Any unused contribution room carried forward from previous years
    # The 2025 annual limit of $7,000
    # Any withdrawals you made in previous years that have been added back to your room
    #
    # Would you like to proceed with making a contribution? I can help you with the next steps if you'd like to contribute some or all of this available room.

    # Example 4: Contribution execution
    if isinstance(state, dict) and state.get("contribution_room") is not None:
        amount = input(f"\nHow much would you like to contribute? (Room: ${state['contribution_room']:.2f}): ")
        user_input = f"My user ID is user_123. Contribute ${amount}"
        logging.info(f"\n=== EXAMPLE 4: Contribution Execution ===\n{user_input}")
        response_text, _ = run_tfsa_assistant_sync(user_input)
        logging.info("\n💎 FINAL RESULT:")
        logging.info(response_text)
