/** 角色数值与 openapi/server 约定一致:Viewer=10、Editor=20、Admin=30、Owner=40 */
export const ROLE = {
  VIEWER: 10,
  EDITOR: 20,
  ADMIN: 30,
  OWNER: 40,
} as const

export function roleLabel(role: number): string {
  switch (role) {
    case ROLE.OWNER:
      return 'Owner'
    case ROLE.ADMIN:
      return 'Admin'
    case ROLE.EDITOR:
      return 'Editor'
    case ROLE.VIEWER:
      return 'Viewer'
    default:
      return `角色${role}`
  }
}

export function isAtLeast(role: number | null | undefined, min: number): boolean {
  return (role ?? -1) >= min
}

export interface RoleOption {
  value: number
  label: string
  desc: string
}

/**
 * 角色下拉可授予的选项:严格低于自己的角色。
 * Admin(30) 只能授 Viewer/Editor;Owner(40) 额外可授 Admin,并通过独立入口(role=40)转让所有权。
 */
export function grantableRoles(myRole: number | null | undefined): RoleOption[] {
  const all: RoleOption[] = [
    { value: ROLE.VIEWER, label: 'Viewer · 只读对话', desc: '仅对话与阅读,只读' },
    { value: ROLE.EDITOR, label: 'Editor · 可上传文档', desc: '上传与管理文档、对话' },
    { value: ROLE.ADMIN, label: 'Admin · 成员管理', desc: '成员管理、空间设置、知识库管理' },
  ]
  return all.filter((o) => o.value < (myRole ?? 0))
}
