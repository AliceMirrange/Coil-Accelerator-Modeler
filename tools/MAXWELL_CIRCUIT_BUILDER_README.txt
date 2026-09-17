Maxwell Circuit 电路生成器
================================

入口：maxwell_accelerator_builder.pyw 的“External Circuit 生成器”页
配置：default.ini
拓扑：支持单路Boost、奇偶Boost与薄膜电容+模拟SCR；Circuit 页为单层布局，拓扑下拉框与工程选项位于同一页面。
SW_V4、VPWL/VPULSE 与位置变量回退参数只在 default.ini 中编辑，GUI 不再显示这些子标签页。

默认工程文件：
E:/Ansys/Maxwell/LinearAcceleratorGenerator/LinearAccelerator_20260807_205932.aedt

固定引脚原则（v0.11.1）
-----------------------
本版以 2026-08-08 实机 Maxwell Circuit Component/Pin Survey 的原始 AEDT 返回为依据。
普通元件全部使用固定 native pin 名；不按 GetComponentPins() 列表位置、屏幕坐标、旋转结果或候选名称推断语义。

固定 pin 映射
-------------
SW_V4：
  n1 = main_1
  n2 = main_2
  n3 = control+
  n4 = control-

VPULSE：
  n1 = positive (+)
  n2 = negative (-)

VPWL：
  n1 = positive (+)
  n2 = negative (-)

Winding：
  n1 = left
  n2 = right

Res：
  n1 = left (0°)
  n2 = right (0°)

DIODE：
  n1 = anode
  n2 = cathode

朝向
----
SW_V4：放置 0° 后连续 3 次 +90° 逆时针 Rotate，不 Flip。
VPWL/VPULSE：保持默认 0°，不执行 Rotate；因此竖直，n1 在上（+），n2 在下（-）。
R_VPULSE：放置 0° 后执行 1 次 +90° 逆时针 Rotate；因此竖直，n2 在上、n1 在下。
Winding / R_coil / DIODE：保持默认 0°。

单路Boost固定接线
-----------------
主回路：
  Winding_n.n2 -> R_coil_n.n1
  R_coil_n.n2   -> DIODE_n.n1
  R_coil_n.n2   -> SW_n.n1
  SW_n.n2       -> Ground

控制回路：
  VPULSE_n.n1 -> SW_n.n3       source+ / control+
  SW_n.n4     -> Ground         control-
  VPULSE_n.n2 -> Ground         source-

VPULSE 并联电阻（阻值由 INI 的 vpulse.shunt_resistance 设置）：
  R_VPULSE_n.n2 -> VPULSE_n.n1  （上端对上端）
  R_VPULSE_n.n1 -> VPULSE_n.n2  （下端对下端）
  R_VPULSE_n.R = default.ini 中的配置值

级联：
  DIODE_n.n2 -> Winding_(n+1).n1
末级返回：
  DIODE_N.n2 -> R_last.n1
  R_last.n2  -> Winding_N.n1
  R_last.R   = $Rlast（前缀及缺失时初值由 default.ini 设置）

奇偶Boost固定级联
-----------------
奇数链：
  DIODE_(2n+1).n2 -> C_storage_(2n+3).n2（电容正极）
偶数链：
  DIODE_(2n).n2   -> C_storage_(2n+2).n2（电容正极）

奇数级显示在上排，偶数级显示在下排，同奇偶级在各自排内从左到右排列。
两条链分别闭合，两个返回电阻共用项目变量 $Rlast：
  最后偶数级 DIODE_(2n).n2   -> R_last_even -> C_storage_(2n).n2
  最后奇数级 DIODE_(2n-1).n2 -> R_last_odd  -> C_storage_(2n-1).n2
奇偶Boost至少需要 2 级，以保证奇数链和偶数链各有一级。

删除的旧逻辑
------------
仍保持删除：
- _switch_semantic_pins()
- _vpulse_polarity_pins()
- _left_right_pin_names() / 按坐标判断端子功能
- _component_pin_names_by_x()
- _require_same_net() / WireId 事后网络一致性检查
- VPULSE 正负端自动防短检查
- validate_single_boost_netlist()
- AcceleratorCircuitOptions.validate_exported_netlist
- 对任何普通多引脚元件的候选名称回退或自动交换。

Ground / GPort
--------------
Ground 是 GPort@... schematic port，原生 GetComponentPins() 可返回空。
仅 Ground 使用 PyAEDT component.pins[0] 提供的唯一 synthetic pin。

VPULSE 参数
-----------
Type = POS
V1   = 0V
V2   = 1.25*$V
Tr   = 0
Tf   = 0
Td   = $POSon_1（第一级）或 $POSon_n-$proj_initZ（第二级起）
Pw   = $POSdur_n
Period 默认 = 1e9mm

第一级改用 Type=POS 的 VPWL，四个点为：
  T1=$POSon_1, V1=1.25*$V
  T2=$POSdur_1, V2=1.25*$V
  T3=$POSdur_1+0.01mm, V3=0V
  T4=1e9mm, V4=0V
第一级 $POSon_1=-1000mm，不优化。

缺失的 $POSon_n / $POSdur_n 一次性批量建立；已存在变量不覆盖。
缺失的 $proj_initZ 默认建立为 0mm；已存在变量不覆盖。
生成器页面和完成对话框提醒：在绘制完动子后，沿Z+方向执行Move操作，Moving Vector为(0,0,$proj_initZ)。

薄膜电容+模拟SCR
-----------------
每一级独立成环，各级之间没有直接连线：
  C_film_n.n2 -> Winding_n.n1
  Winding_n.n2 -> DIODE_n.n1
  DIODE_n.n2 -> SW_n.n1
  SW_n.n2 -> C_film_n.n1 -> Ground

控制回路使用 VPULSE：
  VPULSE_n.n1 -> SW_n.n3
  VPULSE_n.n2 -> SW_n.n4 -> Ground
  第一级 Td=$POSon_1；第二级起 Td=$POSon_n-$proj_initZ。
  Pw=1e9mm，用于模拟 SCR 触发后持续导通。

该拓扑不创建 R_coil_n、R_VPULSE_n、R_ESR_n 或 R_last。

DIODE_Model 参数
----------------
default.ini 的 [diode] 集中配置 model_name、IS、RS、N、EG、XTI、BV、IBV、TNOM。
这些字段名与 AEDT 2024 R2 / PyAEDT 0.25.0 实机 DIODE_Model 属性一致；留空的可选值保留当前 AEDT 元件库默认值。

主要底层 API
------------
- Maxwell2d.create_external_circuit
- MaxwellCircuitComponents.create_component / create_resistor / create_diode / create_gnd
- MaxwellCircuit.export_netlist_from_schematic
- Maxwell2d.edit_external_circuit
- oEditor.GetComponentPinLocation
- MaxwellCircuitComponents.create_wire（底层 oEditor.CreateWire）
- oEditor.Rotate

完整版本历史见 CHANGELOG.txt。


v0.11.1 单路Boost储能支路
------------------------
每级新增 C_storage_n 和 R_ESR_n；二者均放置后执行 1 次 +90° 逆时针 Rotate。
映射表固定：Cap n1=%0、n2=%1；Res n1=%0、n2=%1。旋转一次后 n2 在上、n1 在下。
固定接线：
  Winding_n.n1 -> C_storage_n.n2
  C_storage_n.n1 -> R_ESR_n.n2
  R_ESR_n.n1 -> Ground
其中 n>1 时 Winding_n.n1 与 DIODE_(n-1).n2 已经是同一节点。
C_storage_n.C=$C_n；第 n 级独立使用 `$C_n`。缺失 `$C_n` 默认创建为 1000uF（GUI 可改），已存在变量不覆盖。
C_storage_n.IC=$V；所有级共用 `$V`。缺失 `$V` 默认创建为 0V（GUI 可改），已存在变量不覆盖。
R_ESR_n.R=$ESR_n；缺失变量默认创建为 1mOhm（GUI 可改），已存在变量不覆盖。
储能电容容量不再由 GUI 统一写死；GUI 中的 1000uF 仅作为缺失 `$C_n` 的创建初值。


v0.11.1 Parameter Values 同步
------------------------------
- GUI 按钮“同步 Circuit 全局变量 → Parameter Values”读取已导出的 .sph。
- 先确定 Circuit 实际引用的 `$...` 项目变量，再与 `VariableManager.independent_project_variable_names` 取交集；仅独立项目全局变量以 `参数名 -> 同名变量` 形式传给 `Maxwell2d.edit_external_circuit(parameters=...)`。
- dependent project variable（例如 `$coil_R_n`）明确排除，不会提交。
- 例如 `$C_1` 的 Value 写为 `$C_1`，`$V` 的 Value 写为 `$V`。
- SW_VModel 与 DIODE_Model 的 `DeviceName` 现在显式写成 GUI 指定的共享模型名称，不再保留库默认 `ModelName`。
