import { createContext, useContext } from 'react'

interface AccountCtx {
  accountId: number
  setAccountId: (id: number) => void
}

export const AccountContext = createContext<AccountCtx>({
  accountId: 0,
  setAccountId: () => {},
})

export const useAccount = () => useContext(AccountContext)
