数据来源与用途
1. FairEval原80题、ChatGPT/Vicuna固定回答及human.txt：i-Eval/FairEval，https://github.com/i-Eval/FairEval 。不新生成候选回答。人工标签按固定X=ChatGPT、Y=Vicuna映射；20/60题ID见configs/experiment.json，为本组课程划分。
2. LLMBar外部70题：princeton-nlp/LLMBar，https://github.com/princeton-nlp/LLMBar 。原样保留输入、两回答、原标签、来源子集和行号。Natural35，Neighbor9，GPTInst9，GPTOut9，Manual8；归一化指令去重规则及来源文件哈希见原方案的外部验证70题清单.json。
3. 来源代码、论文和许可按各原仓库说明；LLMBar仓库附MIT LICENSE。数据来源与代码许可应在课程报告中标注，不将仓库许可扩张解释为第三方文本所有权。
4. 模型权重按固定Hugging Face版本下载并校验，许可证随各模型元数据保留，权重不纳入课程提交包。
5. 外部70题不用于阈值、提示或模型挑选；与FairEval分别报告。公开基准不宣称无训练污染。
6. 本目录不是模拟数据。集成模拟数据单独放在服务器formal/integration_fixture_v33，不能合并到正式结果。
