# Telegram-Native UX Rules

## Primary interface

The primary interface is the BotMother Telegram management bot.

Core interactions:
- inline buttons
- reply keyboards
- guided flows
- paginated lists
- preview messages
- confirmation messages
- progress messages
- report files/messages
- quick actions
- commands
- global search
- action palette

## No web-first design

Web/Mini App can support advanced screens, but the main product must work directly inside Telegram.

## Result-first flows

Bad:
- Open module
- Choose technical mode
- Configure many fields
- Start task

Good:
- User selects desired result
- BotMother asks only necessary questions
- BotMother suggests defaults
- BotMother previews result
- User confirms
- BotMother executes and reports

## Main menu example

Home:
- Infrastructure
- Ecosystems
- Operations / Waves
- Visibility
- Import Center
- Check Everything
- Templates / DNA
- Reports
- AI Assistant
- Billing
- Referral
- Settings

## Home screen must show

- infrastructure state
- accounts count
- bots count
- channels count
- groups count
- active waves
- issues requiring attention
- visibility trend
- quick actions

## Menus must not become button dumps

Do not show 30 random buttons.

Menus should have:
- clear purpose
- short explanation
- main actions
- advanced actions hidden under Advanced
- Back
- Help
- Search/filter where needed
- status summary where useful

## Child-simple explanations

Every feature should explain:
1. What this does
2. Why it is useful
3. What the user needs to provide
4. What happens after pressing
5. What can go wrong
6. How to retry/fix

Example:

Bad:
“Bulk metadata synchronization”

Good:
“Update channel setup”
“This changes the name, description, avatar, or links for many channels at once. Before anything changes, you will see exactly which channels will be updated.”

## Complex flow structure

For complex actions:
1. Explain
2. Collect input
3. Show preview
4. Ask confirmation
5. Execute
6. Show progress
7. Show result
8. Offer retry/report/repeat
