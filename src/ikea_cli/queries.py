"""GraphQL documents for the purchase-history and favourites services."""

DATE_FRAGMENT = """
fragment DateAndTime on DateAndTime {
  date
  time
  timeZone
  formattedShortDate
  formattedLongDateTime
}
"""

PURCHASE_HISTORY = (
    """
query FullHistory($skip: Int!, $take: Int!, $articleTake: Int = 8) {
  historyData(skip: $skip, take: $take) {
    totalPurchases
    historicalPurchases {
      id
      status
      eventStatus
      type
      storeName
      dateAndTime { ...DateAndTime }
      totalCost { code value formatted }
      articles {
        quantity
        any(skip: 0, take: $articleTake) { id name description quantity itemType }
      }
    }
  }
}
"""
    + DATE_FRAGMENT
)

_PURCHASE_DETAIL_FIELDS = """
    id
    status
    storeName
    dateAndTime { ...DateAndTime }
    costs {
      total { formatted value code }
      sub { formatted value code }
      delivery { formatted value code }
      service { formatted value code }
      discount { formatted value code }
      tax { formatted value code }
    }
    articles {
      ... on AnyDirectionProducts {
        quantity
        any(skip: $skip, take: $take) {
          id
          name
          description
          quantity
          itemType
          href
          unitPrice { formatted }
          totalPrice { formatted }
        }
      }
    }
"""

ORDER_DETAIL = (
    """
query OrderDetail($orderNumber: String!, $skip: Int!, $take: Int!) {
  order(orderNumber: $orderNumber) {
"""
    + _PURCHASE_DETAIL_FIELDS
    + """
    deliveryMethods {
      type
      statusV2 { deliveryStatusV2 tense }
      deliveryDate {
        actual { ...DateAndTime }
        estimatedFrom { ...DateAndTime }
        estimatedTo { ...DateAndTime }
        dateTimeRange
      }
    }
  }
}
"""
    + DATE_FRAGMENT
)

RECEIPT_DETAIL = (
    """
query ReceiptDetail($receiptNumber: String!, $skip: Int!, $take: Int!) {
  receipt(receiptNumber: $receiptNumber) {
"""
    + _PURCHASE_DETAIL_FIELDS
    + """
  }
}
"""
    + DATE_FRAGMENT
)

ORDER_STATUS = (
    """
query OrderStatus($orderNumber: String!) {
  order(orderNumber: $orderNumber) {
    id
    status
    deliveryMethods {
      type
      statusV2 { deliveryStatusV2 tense }
      deliveryDate {
        actual { ...DateAndTime }
        estimatedFrom { ...DateAndTime }
        estimatedTo { ...DateAndTime }
        dateTimeRange
      }
    }
  }
}
"""
    + DATE_FRAGMENT
)

# Favourites ----------------------------------------------------------------

LIST_FRAGMENT = """
fragment ListFields on List {
  listId
  name
  isSelected
  updated
  quantity
  items {
    itemNo
    quantity
    addedAt
    type
    isBuyableOnline
    product {
      name
      description
      globalName
    }
    prices {
      online {
        regular { unit { inclTax exclTax } }
        family { unit { inclTax exclTax } }
      }
    }
  }
}
"""

_PRESENTATION = "itemPresentation: {languageCode: $languageCode, storeId: $storeId, zip: $zip}"
_VARS = "$languageCode: String!, $storeId: String, $zip: String"

FAV_LISTS = f"query GetLists({_VARS}) {{ lists({_PRESENTATION}) {{ ...ListFields }} }}" + LIST_FRAGMENT
FAV_LIST = f"query GetList($listId: ID, {_VARS}) {{ list(listId: $listId, {_PRESENTATION}) {{ ...ListFields }} }}" + LIST_FRAGMENT
FAV_CREATE = "mutation CreateList($name: String!) { list: createList(name: $name) { listId name isSelected updated quantity } }"
FAV_RENAME = (
    f"mutation RenameList($listId: ID!, $name: String!, {_VARS}) "
    f"{{ list: renameList(listId: $listId, name: $name, {_PRESENTATION}) {{ ...ListFields }} }}" + LIST_FRAGMENT
)
FAV_DELETE = (
    f"mutation RemoveList($listId: ID!, {_VARS}) {{ lists: removeList(listId: $listId, {_PRESENTATION}) {{ ...ListFields }} }}"
    + LIST_FRAGMENT
)
FAV_SELECT = (
    f"mutation SetSelected($listId: ID!, {_VARS}) {{ lists: setSelected(listId: $listId, {_PRESENTATION}) {{ ...ListFields }} }}"
    + LIST_FRAGMENT
)
FAV_ADD = (
    f"mutation AddItems($items: [AddItemInput!]!, $listId: ID, $ignoreUnavailable: Boolean!, {_VARS}) "
    f"{{ list: addItems(items: $items, listId: $listId, ignoreUnavailable: $ignoreUnavailable, {_PRESENTATION}) {{ ...ListFields }} }}"
    + LIST_FRAGMENT
)
FAV_UPDATE = (
    f"mutation UpdateItems($items: [UpdateItemInput!]!, $listId: ID, {_VARS}) "
    f"{{ list: updateItems(items: $items, listId: $listId, {_PRESENTATION}) {{ ...ListFields }} }}" + LIST_FRAGMENT
)
FAV_REMOVE = (
    f"mutation RemoveItems($itemNos: [ID!]!, $listId: ID, {_VARS}) "
    f"{{ list: removeItems(itemNos: $itemNos, listId: $listId, {_PRESENTATION}) {{ ...ListFields }} }}" + LIST_FRAGMENT
)
