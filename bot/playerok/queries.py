"""GraphQL-запросы к playerok.com.

У Playerok нет публичного API: сайт сам ходит в https://playerok.com/graphql,
и бот повторяет те же запросы. Названия операций и поля ниже собраны по тому,
как работает сайт, но площадка может их менять. Если какой-то запрос перестал
работать, открой playerok.com в браузере → DevTools → Network → фильтр
"graphql", найди нужную операцию и поправь текст запроса здесь. Больше нигде
в коде запросы не описаны.
"""

# --- Авторизация по почте: сначала код на email, потом его подтверждение ---

GET_EMAIL_AUTH_CODE = """
mutation getEmailAuthCode($email: String!) {
  getEmailAuthCode(input: { email: $email })
}
"""

CHECK_EMAIL_AUTH_CODE = """
mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {
  checkEmailAuthCode(input: $input) {
    id
    username
    email
  }
}
"""

# --- Текущий пользователь ---

VIEWER = """
query viewer {
  viewer {
    id
    username
    email
    balance {
      value
      available
    }
  }
}
"""

# --- Сделки (продажи текущего пользователя) ---

DEALS = """
query deals($pagination: Pagination, $filter: ItemDealFilter) {
  deals(pagination: $pagination, filter: $filter) {
    edges {
      node {
        id
        status
        direction
        createdAt
        item {
          id
          slug
          name
          price
        }
        user {
          id
          username
        }
        chat {
          id
        }
        review {
          id
          rating
          text
        }
      }
    }
  }
}
"""

# --- Лоты продавца, поднятие и перевыставление ---
# Поднятие на Playerok платное (покупка статуса приоритета), поэтому в боте
# есть суточный лимит. Название мутации и аргументы сверить в DevTools.

MY_ITEMS = """
query items($pagination: Pagination, $filter: ItemFilter) {
  items(pagination: $pagination, filter: $filter) {
    edges {
      node {
        id
        slug
        name
        price
        status
        priorityPosition
        priorityStatus {
          id
          price
        }
      }
    }
  }
}
"""

INCREASE_ITEM_PRIORITY = """
mutation increaseItemPriorityStatus($input: PublishItemInput!) {
  increaseItemPriorityStatus(input: $input) {
    id
    priorityPosition
  }
}
"""

PUBLISH_ITEM = """
mutation publishItem($input: PublishItemInput!) {
  publishItem(input: $input) {
    id
    status
  }
}
"""

UPDATE_DEAL = """
mutation updateDeal($input: UpdateItemDealInput!) {
  updateDeal(input: $input) {
    id
    status
  }
}
"""

# --- Чаты и сообщения ---

CHATS = """
query chats($pagination: Pagination, $filter: ChatFilter) {
  chats(pagination: $pagination, filter: $filter) {
    edges {
      node {
        id
        unreadMessagesCounter
        lastMessage {
          id
          text
          createdAt
          user {
            id
            username
          }
        }
        participants {
          id
          username
        }
      }
    }
  }
}
"""

CREATE_CHAT_MESSAGE = """
mutation createChatMessage($input: CreateChatMessageInput!) {
  createChatMessage(input: $input) {
    id
    text
    createdAt
  }
}
"""

MARK_CHAT_AS_READ = """
mutation markChatAsRead($input: MarkChatAsReadInput!) {
  markChatAsRead(input: $input) {
    id
    unreadMessagesCounter
  }
}
"""
