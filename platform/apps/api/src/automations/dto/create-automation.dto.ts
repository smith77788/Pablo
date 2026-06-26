export class CreateAutomationDto {
  name: string;
  triggerType: string;   // 'message_received' | 'keyword' | 'user_joined'
  keyword?: string;
  actionType: string;    // 'send_message' | 'add_tag' | 'webhook'
  actionPayload: string; // message text, tag name, or webhook URL
  isActive?: boolean;
}
