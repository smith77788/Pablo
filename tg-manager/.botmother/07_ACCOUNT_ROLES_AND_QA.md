# Account Roles & Controlled Internal QA Workflows

## Account roles

Connected accounts may have roles:
- Owner account
- Publisher account
- Moderator account
- Support account
- Backup account
- Regional account
- Test account
- Observer account
- Bot Manager account
- Channel Manager account
- Group Manager account

Roles organize legitimate workflows:
- publishing
- support
- moderation
- testing
- regional management
- backup operations
- bot management
- channel management

## Account role matrix

For each account store:
- role
- region
- language
- ecosystem
- owned assets
- admin assets
- allowed actions
- preferred use cases
- current load
- health
- permissions
- last used in scenarios
- linked workflows

## Controlled internal dialogues / workflows

BotMother may support controlled role-based dialogues between connected accounts only for legitimate purposes:
- testing bot flows
- testing onboarding sequences
- testing support workflows
- checking group permissions
- checking message delivery
- verifying templates
- simulating operator handoff
- internal QA
- team coordination
- moderation testing

Must not create deceptive fake engagement, spam, or misleading public activity.

Any account-to-account interaction feature must be:
- clearly labeled as testing / internal workflow / QA
- permission-based
- previewable
- logged
- limited
- transparent to workspace owner
- never used to mislead real users

## Scenario builder

Example:
“Test this bot onboarding flow”

Roles:
- Account A = new user/tester
- Account B = support operator
- Bot = tested bot

Flow:
1. Account A starts bot.
2. Bot sends welcome message.
3. Account A clicks button.
4. Support account receives relay.
5. Account B replies.
6. System records result.

Use cases:
- test bot menus
- test auto replies
- test funnels
- test relay/inbox
- test group welcome flow
- test permissions
- test moderation flow
- test publishing setup
