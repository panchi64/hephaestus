import type { BuiltinPluginUi } from "../types";

export const mailPlugin: BuiltinPluginUi = {
  name: "mail",
  nav: [
    {
      label: "Email",
      href: "/email",
      icon: "mail",
      description: "Read and send email",
      connected: true,
    },
  ],
  toolCategory: {
    name: "mail",
    icon: "mail",
    preparing: "Preparing a mail action",
  },
  tools: {
    mail_draft_reply: {
      icon: "pen",
      label: "Draft reply",
      keys: ["message_id"],
      preparing: "Drafting a reply",
    },
    mail_list_accounts: {
      icon: "mail",
      label: "Mail accounts",
      noun: ["account", "accounts"],
      preparing: "Listing the mail accounts",
    },
    mail_list_messages: {
      icon: "mail",
      label: "Inbox",
      keys: ["folder", "account_id"],
      noun: ["message", "messages"],
      preparing: "Choosing a mailbox to list",
    },
    mail_mark: {
      icon: "mail",
      label: "Mark mail",
      keys: ["message_id"],
      preparing: "Marking a message",
    },
    mail_read: {
      icon: "mail",
      label: "Read mail",
      keys: ["message_id"],
      preparing: "Opening a message",
    },
    mail_reply: {
      icon: "send",
      label: "Reply",
      keys: ["explanation", "message_id"],
      preparing: "Writing a reply",
    },
    mail_send: {
      icon: "send",
      label: "Send mail",
      keys: ["subject", "to"],
      preparing: "Writing an email",
    },
  },
};
