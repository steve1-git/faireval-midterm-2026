"""Deterministic aggregation of saved formal generations; never invokes a judge."""
from pathlib import Path
import sys,json,time,sqlite3
import numpy as np
from experiment import load,read,save,Journal
from protocol import aggregate_selected
from analysis_helpers import question_sensitivity,skip_invalid_diagnostic,author_compatible_aggregate

def interval(values,groups=None,seed=20261023,n=5000):
    values=np.asarray(values,dtype=float);rng=np.random.default_rng(seed)
    if len(values)<2:return None
    if groups is None:indices=rng.integers(0,len(values),size=(n,len(values)))
    else:
        indices=np.concatenate([rng.choice(np.where(np.array(groups)==key)[0],size=(n,sum(x==key for x in groups)),replace=True) for key in sorted(set(groups))],axis=1)
    samples=values[indices].mean(axis=1)
    return [float(x) for x in np.quantile(samples,[.025,.975])]
def pvalue(values):
    a=np.asarray(values,dtype=float);rng=np.random.default_rng(20261024)
    observed=abs(a.mean());centered=a-a.mean();null=centered[rng.integers(0,len(a),size=(20000,len(a)))].mean(axis=1)
    return float((1+np.count_nonzero(np.abs(null)>=observed))/(len(null)+1))
def holm(p):
    names=sorted(p,key=p.get);out={};previous=0
    for i,key in enumerate(names):previous=max(previous,(len(names)-i)*p[key]);out[key]=min(1.,previous)
    return out

def analyze(root):
    root=Path(root);cfg,mapping,orientations,items=load(root);layout_by={x['layout']:x for x in cfg['layouts']}
    qindex={key:i for i,key in enumerate(orientations['question_ids_in_bit_order'])}
    all_predictions={};all_summary={};all_cases=[];global_counts={};all_mechanisms={};manipulation={}
    def jid(slug,qid,kind,block,layout,draw=1):return f'{slug}|{qid}|{kind}|b{block}|{layout}|d{draw}'
    for model in cfg['models']:
        slug=model['model'].split('/')[-1];dbfile=root/'results'/f'state_{slug}.sqlite'
        if not dbfile.exists():continue
        journal=Journal(dbfile);records=journal.records();counts=journal.counts();global_counts[slug]=counts
        def selected(qid,block,refs,kind='mc',policy='strict'):
            rows=[records[jid(slug,qid,kind,block,ref['layout'],ref['draw'])] for ref in refs]
            if any(row['state'] not in ['completed_valid','completed_invalid'] for row in rows):return {'state':'pending','prediction':None}
            parsed=[{**row['parsed'],'layout':row['spec']['layout']} for row in rows]
            if policy=='strict':result=aggregate_selected(parsed)
            elif policy=='skip':result=skip_invalid_diagnostic(parsed)
            else:result=author_compatible_aggregate([{'text':row['raw']['text'],'label_X':layout_by[row['spec']['layout']]['labels']['X']} for row in rows])
            return {**result,'state':'completed_valid' if result['valid'] else 'completed_invalid','prediction':result.get('decision'),'job_ids':[row['spec']['job_id'] for row in rows],
                    'completion_tokens':sum(row['raw']['completion_tokens'] for row in rows),'selected_calls':len(rows),'truncated_calls':sum(row['raw']['finish_reason']=='length' for row in rows)}
        def mix(first,second,w):return {'mixture':[[w,first],[1-w,second]],'state':'pending' if (w and first['state']=='pending') or (w<1 and second['state']=='pending') else 'completed'}
        predictions={};mechanisms={}
        for qid,item in items.items():
            blocks=[]
            for block in range(1,4):
                methods={key:selected(qid,block,refs) for key,refs in mapping['extension'].items()}
                w=sum(bits[qindex[qid]]=='1' for bits in orientations['blocks'][str(block)])/100
                methods['A']=mix(methods['A_canonical'],methods['A_reverse'],w)
                for method in ['A_canonical','A_reverse','B','C','D']:
                    methods[method+'_skip']=selected(qid,block,mapping['extension'][method],policy='skip')
                methods['A_skip']=mix(methods['A_canonical_skip'],methods['A_reverse_skip'],w)
                for kind,name in [('vanilla','Vanilla'),('ec','EC')]:
                    f=selected(qid,0,[{'layout':cfg['standard_BPC_layouts'][0],'draw':1}],kind)
                    r=selected(qid,0,[{'layout':cfg['standard_BPC_layouts'][1],'draw':1}],kind)
                    methods[name]=mix(f,r,w);methods[name+'_forward']=f;methods[name+'_reverse']=r
                    if kind=='ec':
                        refs=[{'layout':key,'draw':1} for key in cfg['standard_BPC_layouts']]
                        methods['EC_BPC']=selected(qid,0,refs,'ec')
                        methods['EC_author']=mix(selected(qid,0,refs[:1],'ec','author'),selected(qid,0,refs[1:],'ec','author'),w)
                for size in [3,6]:
                    name='MEC'+str(size)
                    f=mapping['original_style_sampling'][name+'_canonical'];r=mapping['original_style_sampling'][name+'_reverse']
                    methods[name]=mix(selected(qid,block,f),selected(qid,block,r),w)
                    methods[name+'_author']=mix(selected(qid,block,f,policy='author'),selected(qid,block,r,policy='author'),w)
                refs=mapping['original_style_sampling']['MEC3_BPC3']
                methods['MEC3_BPC3']=selected(qid,block,refs)
                methods['MEC3_BPC3_author']=selected(qid,block,refs,policy='author')
                blocks.append(methods)
            predictions[qid]={'dataset':item['dataset'],'subset':item['subset'],'gold':item['gold'],'blocks':blocks}
            core=[]
            for block in range(1,4):
                cells={}
                for layout in layout_by:
                    row=records[jid(slug,qid,'mc',block,layout)]
                    cells[layout]=row['parsed'] if row['parsed'] is not None else {'valid':False}
                core.append(cells)
            mechanisms[qid]=question_sensitivity(cfg['layouts'],core)
        all_predictions[slug]=predictions;all_mechanisms[slug]=mechanisms
        def outcome(result,gold):
            if 'mixture' in result:
                parts=[(weight,outcome(value,gold)) for weight,value in result['mixture'] if weight]
                if any(x['ready'] is False for _,x in parts):return {'ready':False}
                return {'ready':True,'valid':sum(w*x['valid'] for w,x in parts),'correct':sum(w*x['correct'] for w,x in parts),'probs':{label:sum(w*x['probs'].get(label,0) for w,x in parts) for label in ['X','Y','TIE','INVALID']},**{key:sum(w*x[key] for w,x in parts) for key in ['completion_tokens','selected_calls','truncated_calls']}}
            if result['state']=='pending':return {'ready':False}
            valid=result['state']=='completed_valid';label=result['prediction'] if valid else 'INVALID'
            return {'ready':True,'valid':float(valid),'correct':float(valid and label==gold),'probs':{label:1.},**{key:result[key] for key in ['completion_tokens','selected_calls','truncated_calls']}}
        sets={'FairEval_test':[q for q,x in items.items() if x['dataset']=='FairEval' and x['subset']=='test'],
              'FairEval_calibration':[q for q,x in items.items() if x['dataset']=='FairEval' and x['subset']=='calibration'],
              'FairEval_all80':[q for q,x in items.items() if x['dataset']=='FairEval'],
              'LLMBar_balanced70':[q for q,x in items.items() if x['dataset']=='LLMBar']}
        sets.update({'LLMBar_'+s:[q for q,x in items.items() if x['dataset']=='LLMBar' and x['subset']==s] for s in ['natural','neighbor','gptinst','gptout','manual']})
        sets['LLMBar_adversarial']=[q for q,x in items.items() if x['dataset']=='LLMBar' and x['subset']!='natural']
        model_summary={}
        method_names=['Vanilla','EC','EC_BPC','MEC3','MEC6','MEC3_BPC3','A','B','C','D','A_skip','B_skip','C_skip','D_skip','EC_author','MEC3_author','MEC6_author','MEC3_BPC3_author']
        for set_name,qids in sets.items():
            report={};vectors={}
            for name in method_names:
                qvalues={};completed=[]
                for qid in qids:
                    cells=[outcome(block[name],items[qid]['gold']) for block in predictions[qid]['blocks']]
                    completed.extend(x for x in cells if x['ready'])
                    if all(x['ready'] for x in cells):qvalues[qid]=sum(x['correct'] for x in cells)/3
                n=len(qids)*3;finished=len(completed);valid=sum(x['valid'] for x in completed);correct=sum(x['correct'] for x in completed)
                row={'scheduled_question_blocks':n,'completed_question_blocks':finished,'finalizable':finished==n,
                     'strict_accuracy':correct/n if finished==n else None,'valid_decision_rate':valid/n if finished==n else None,
                     'conditional_valid_accuracy':correct/valid if valid and finished==n else None,
                     'provisional_completed_subset':{'n':finished,'correct_output_rate':correct/finished if finished else None,'valid_rate':valid/finished if finished else None}}
                if finished==n:
                    row['mean_completion_tokens_per_question_block']=sum(x['completion_tokens'] for x in completed)/n
                    row['truncated_call_fraction']=sum(x['truncated_calls'] for x in completed)/sum(x['selected_calls'] for x in completed)
                    probs={label:sum(x['probs'].get(label,0) for x in completed)/n for label in ['X','Y','TIE','INVALID']}
                    row['predicted_proportions']=probs
                    confusion={gold:{pred:0. for pred in ['X','Y','TIE','INVALID']} for gold in ['X','Y','TIE']}
                    for qid in qids:
                        for block in predictions[qid]['blocks']:
                            out=outcome(block[name],items[qid]['gold'])
                            for pred,weight in out['probs'].items():confusion[items[qid]['gold']][pred]+=weight
                    row['confusion']=confusion
                    gt_valid={gold:sum(v[pred] for pred in ['X','Y','TIE']) for gold,v in confusion.items()}
                    pred_valid={pred:sum(confusion[g][pred] for g in confusion) for pred in ['X','Y','TIE']}
                    pe=sum(gt_valid[k]*pred_valid[k] for k in ['X','Y','TIE'])/valid**2 if valid else None
                    row['conditional_valid_kappa']=(correct/valid-pe)/(1-pe) if valid and pe<1 else None
                    tie_tp=confusion['TIE']['TIE'];tie_preds=sum(confusion[g]['TIE'] for g in confusion)
                    row['tie_precision']=tie_tp/tie_preds if tie_preds else None
                    tie_gold=sum(confusion['TIE'].values());row['tie_recall']=tie_tp/tie_gold if tie_gold else None
                    row['strict_accuracy_CI95']=interval([qvalues[q] for q in qids],[items[q]['subset'] for q in qids] if set_name.startswith('LLMBar') else None)
                    vectors[name]=qvalues
                report[name]=row
            contrasts={}
            for other in ['B','C','A']:
                if 'D' in vectors and other in vectors:
                    delta=[vectors['D'][q]-vectors[other][q] for q in qids]
                    contrasts['D-'+other]={'difference':float(np.mean(delta)),'CI95':interval(delta,[items[q]['subset'] for q in qids] if set_name.startswith('LLMBar') else None),'independent_questions':len(qids)}
                    if set_name=='FairEval_test' and other in ['C','A']:contrasts['D-'+other]['p_approx_centered_bootstrap']=pvalue(delta)
            if set_name=='FairEval_test' and all(k in contrasts for k in ['D-C','D-A']):
                adjusted=holm({key:contrasts[key]['p_approx_centered_bootstrap'] for key in ['D-C','D-A']})
                for key,value in adjusted.items():contrasts[key]['p_Holm_two_secondary_methods']=value
            eligible=[q for q in qids if mechanisms[q]['eligible']]
            mechanism_report={'eligible_questions':len(eligible),'scheduled_questions':len(qids),'conditional_complete24_diagnostic':True,'provisional':not report['D']['finalizable']}
            if eligible:
                terms=['P','L','S','PL','PS','LS','PLS'];effects={};raw_p={}
                for term in terms:
                    values=[mechanisms[q]['question_mean_coefficients'][term] for q in eligible]
                    effects[term]={'mean_signed_beta':float(np.mean(values)),'CI95':interval(values,[items[q]['subset'] for q in eligible] if set_name.startswith('LLMBar') else None),'per_question_values':dict(zip(eligible,values))}
                    if set_name=='FairEval_test' and not mechanism_report['provisional'] and len(values)>=2:raw_p[term]=pvalue(values)
                if raw_p:
                    adjusted=holm(raw_p)
                    for key in raw_p:effects[key].update(p_approx_centered_bootstrap=raw_p[key],p_Holm_seven_coefficients=adjusted[key])
                mechanism_report['effects']=effects;mechanism_report['sensitivity']={}
                for factor in ['P','L','S']:
                    flips=[mechanisms[q]['single_factor_flip'][factor]['conflict_rate'] for q in eligible]
                    repeat=[mechanisms[q]['same_layout_repeat']['conflict_rate'] for q in eligible]
                    excess=[a-b for a,b in zip(flips,repeat)]
                    groups=[items[q]['subset'] for q in eligible] if set_name.startswith('LLMBar') else None
                    mechanism_report['sensitivity'][factor]={'flip_conflict_rate':float(np.mean(flips)),'repeat_conflict_rate':float(np.mean(repeat)),'excess':float(np.mean(excess)),'flip_CI95':interval(flips,groups),'repeat_CI95':interval(repeat,groups),'marginal_excess_CI95':interval(excess,groups),'descriptive_not_extra_confirmatory_test':True}
            coverage={};complete_eight=0
            for bi in range(1,4):
                coverage[str(bi)]={}
                for layout in layout_by:
                    rows=[records[jid(slug,q,'mc',bi,layout)] for q in qids]
                    failures={}
                    for row in rows:
                        if row['state']=='completed_invalid':
                            for reason in row['parsed']['reasons']:failures[reason]=failures.get(reason,0)+1
                    coverage[str(bi)][layout]={'scheduled':len(rows),'valid':sum(row['state']=='completed_valid' for row in rows),'invalid':sum(row['state']=='completed_invalid' for row in rows),'failure_reasons':failures}
                complete_eight+=sum(all(records[jid(slug,q,'mc',bi,layout)]['state']=='completed_valid' for layout in layout_by) for q in qids)
            mechanism_report['coverage']={'scheduled_eight_cell_blocks':len(qids)*3,'complete_eight_cell_blocks':complete_eight,'complete24_questions':len(eligible),'by_block_and_layout':coverage}
            joint=[]
            for qid in qids:
                for block_index,block in enumerate(predictions[qid]['blocks'],1):
                    b,d=block['B'],block['D']
                    if b['state']==d['state']=='completed_valid':joint.append({'question_id':qid,'block':block_index,'correct_B':int(b['prediction']==items[qid]['gold']),'correct_D':int(d['prediction']==items[qid]['gold'])})
            joint_report={'common_valid_units':len(joint),'common_valid_questions':len({x['question_id'] for x in joint}),'conditional_selection_remains':True,'difference':sum(x['correct_D']-x['correct_B'] for x in joint)/len(joint) if joint else None,'paired_units':joint,'provisional':not report['B']['finalizable'] or not report['D']['finalizable']}
            joint_report.update(agreement_B=sum(x['correct_B'] for x in joint)/len(joint) if joint else None,agreement_D=sum(x['correct_D'] for x in joint)/len(joint) if joint else None)
            report['B_D_jointly_valid']=joint_report
            failure_diagnostics={}
            for name in ['A','B','C','D']:
                changed=retained=skipped=finished=0.;patterns={}
                for qid in qids:
                    for block in predictions[qid]['blocks']:
                        original,diagnostic=block[name],block[name+'_skip']
                        pairs=[(w,a,b) for (w,a),(_,b) in zip(original['mixture'],diagnostic['mixture'])] if name=='A' else [(1.,original,diagnostic)]
                        for weight,a,b in pairs:
                            if not weight or a['state']=='pending' or b['state']=='pending':continue
                            finished+=weight;retained+=weight*b['retained_calls'];skipped+=weight*b['skipped_calls']
                            ap=a['prediction'] if a['state']=='completed_valid' else 'INVALID';bp=b['prediction'] if b['state']=='completed_valid' else 'INVALID'
                            changed+=weight*(ap!=bp)
                            key=json.dumps(b['retained_calls_by_layout'],sort_keys=True)
                            patterns[key]=patterns.get(key,0.)+weight
                failure_diagnostics[name]={'scheduled_question_blocks':len(qids)*3,'completed_question_blocks':finished,'changed_decision_units':changed,'mean_retained_calls':retained/finished if finished else None,'mean_skipped_calls':skipped/finished if finished else None,'remaining_layout_patterns':patterns,'strict_accuracy':report[name]['strict_accuracy'],'skip_diagnostic_accuracy':report[name+'_skip']['strict_accuracy'],'direction_mixture_weighted':name=='A','reduced_layout_diagnostic_not_full_protocol':True}
            failure_diagnostics['D_minus_B']={'strict':contrasts.get('D-B',{}).get('difference'),'skip_diagnostic':report['D_skip']['strict_accuracy']-report['B_skip']['strict_accuracy'] if report['D_skip']['finalizable'] and report['B_skip']['finalizable'] else None}
            # Eight equally sampled cells for manipulation checks; extra repetitions excluded.
            check_layouts={}
            for layout in layout_by:
                calls=[records[jid(slug,q,'mc',b,layout)] for q in qids for b in range(1,4)]
                parsed=[x['parsed'] for x in calls if x['parsed'] is not None]
                readable=[x for x in parsed if x.get('actual_S') is not None]
                check_layouts[layout]={'scheduled':len(calls),'scored':len(parsed),'readable_score_pairs':len(readable),
                    'conditional_order_compliance':sum(x['order_compliant'] for x in readable)/len(readable) if readable else None,
                    'valid_and_compliant_count':sum(x['valid'] and x['order_compliant'] for x in readable),'truncated_count':sum(x.get('truncated',False) for x in parsed)}
            position_pairs=[]
            for qid in qids:
                unit=predictions[qid]['blocks'][0];a,b=unit['Vanilla_forward'],unit['Vanilla_reverse']
                if a['state']==b['state']=='completed_valid':position_pairs.append((a['prediction'],b['prediction']))
            position={'valid_pairs':len(position_pairs),'scheduled_pairs':len(qids),'conflict_rate':sum(a!=b for a,b in position_pairs)/len(position_pairs) if position_pairs else None,
                      'opposite_winner_rate':sum({a,b}=={'X','Y'} for a,b in position_pairs)/len(position_pairs) if position_pairs else None}
            score_order_groups={}
            for sign in [1,-1]:
                cells=[row for key,row in check_layouts.items() if layout_by[key]['S']==sign]
                rates=[row['conditional_order_compliance'] for row in cells]
                score_order_groups[str(sign)]={'scheduled':sum(row['scheduled'] for row in cells),'scored':sum(row['scored'] for row in cells),'readable_score_pairs':sum(row['readable_score_pairs'] for row in cells),
                    'macro_conditional_compliance':sum(rates)/4 if all(value is not None for value in rates) else None,
                    'valid_and_compliant_count':sum(row['valid_and_compliant_count'] for row in cells),'layouts_with_readable_data':sum(value is not None for value in rates)}
            model_summary[set_name]={'methods':report,'contrasts':contrasts,'mechanism':mechanism_report,'manipulation_balanced_core':check_layouts,'manipulation_assigned_S':score_order_groups,'vanilla_position_conflict':position,'same_parser_failure_policy_diagnostic':failure_diagnostics}
        all_summary[slug]=model_summary
        # Evidence cases contain exact block decisions; semantic explanation remains reviewable.
        for qid in sorted(items,key=lambda q:(items[q]['dataset']!='FairEval',items[q]['subset']!='test',q)):
            item=items[qid]
            for bi,block in enumerate(predictions[qid]['blocks'],1):
                b,d=block['B'],block['D']
                if b['state']=='pending' or d['state']=='pending':continue
                gold=item['gold'];bc=b.get('prediction')==gold and b['state']=='completed_valid';dc=d.get('prediction')==gold and d['state']=='completed_valid'
                kind='D纠正B' if dc and not bc else 'D引入错误' if bc and not dc else '双方正确' if bc and dc else '双方未匹配人工参照'
                if any(x['model']==slug and x['question_id']==qid for x in all_cases):continue
                all_cases.append({'model':slug,'question_id':qid,'block':bi,'dataset':item['dataset'],'subset':item['subset'],'question':item['question'],'X':item['X'],'Y':item['Y'],'answer_length_characters':{'X':len(item['X']),'Y':len(item['Y']),'X_minus_Y':len(item['X'])-len(item['Y'])},'human':gold,'kind':kind,'B':b,'D':d,'evidence_note':f'此区组人工参照={gold}，B={b.get("prediction")}，D={d.get("prediction")}；完整逐次证据按job_ids查raw分片；先区分协议无效与有效判断错误，再核验具体语义原因。','raw_entry_directory':'results/raw/'+slug})
        journal.db.close()
    chosen=[]
    for slug in all_summary:
        candidates=[x for x in all_cases if x['model']==slug];picked=[];used=set()
        for kind in ['D纠正B','D引入错误','双方未匹配人工参照','双方正确']:
            for case in [x for x in candidates if x['kind']==kind][:3]:
                if case['question_id'] not in used:picked.append(case);used.add(case['question_id'])
        for case in candidates:
            if len(picked)>=12:break
            if case['question_id'] not in used:picked.append(case);used.add(case['question_id'])
        chosen.extend(picked)
    all_cases=chosen
    terminal=sum(sum(v.get(k,0) for k in ['completed_valid','completed_invalid']) for v in global_counts.values())
    summary={'planned_unique_tasks':22800,'terminal_tasks':terminal,'global_complete':terminal==22800,'counts':global_counts,'models':all_summary,'updated_at':time.time(),'primary':'Qwen2.5-72B-Instruct / FairEval_test / D-B strict accuracy','invalid_execution_is_not_a_model_error':True}
    save(root/'results/summary.json',summary);save(root/'results/predictions.json',all_predictions);save(root/'results/cases.json',all_cases);save(root/'results/mechanism_cases.json',all_mechanisms)
    text=['# FairEval v3.3 正式实验中期记录','',f'已持久化并完成评分：{terminal}/22,800。尚未完成的任务不作为模型错误，也不发布其最终准确率。',
          '原文定位：§2.3为EC/MEC/BPC，§3.2为解码和初始方向统计，Table2为位置冲突，Table4为人工一致性比较。',
          '设置差异：使用固定Qwen2.5-72B与Qwen3-32B本地BF16裁判；FairEval20/60为本组划分；另有冻结LLMBar70；输出上限2048；严格完整协议；八布局为本组扩展。',
          '真实并列结果见results/summary.json；逐题判决见results/predictions.json，逐次原始输出见results/raw/，实际模板及token IDs见results/prompts_*.jsonl。',
          '当前阶段结果仅使用正式输出，未以校准重放或旧3B成绩替代。完整协议正确率不单独代表语义能力；同时报告有效率与条件准确率。','']
    for slug,reports in all_summary.items():
        text+=['## '+slug,'','| 数据/方法 | 完成题块 | 严格准确率 | 有效率 | 有效条件准确率 |','|---|---:|---:|---:|---:|']
        for dataset in ['FairEval_test','LLMBar_balanced70']:
            for method in ['Vanilla','EC','MEC3','MEC6','MEC3_BPC3','A','B','C','D']:
                row=reports[dataset]['methods'][method]
                def fmt(value):return f'{value:.4f}' if value is not None else '待完整'
                text.append(f'| {dataset}/{method} | {row["completed_question_blocks"]}/{row["scheduled_question_blocks"]} | {fmt(row["strict_accuracy"])} | {fmt(row["valid_decision_rate"])} | {fmt(row["conditional_valid_accuracy"])} |')
    text+=['','## 失败或边界证据','']
    selected_cases=[x for x in all_cases if x['kind']!='双方正确'][:2]
    for case in selected_cases:text.append(f'- {case["model"]} / {case["question_id"]} / 区组{case["block"]}：{case["kind"]}。{case["evidence_note"]}')
    if len(selected_cases)<2:text.append('目前尚未形成两例完整失败/边界；形成后由正式输出补入，不编造案例。')
    text+=['','下一项单因素比较：以八布局核心数据固定另外两因素，比较P/L/S单因素翻转与同布局重复；D-C解除标签/评分顺序绑定的消融另列。',
           '三个随机区组与100次初始方向分配均不扩大独立题目数量。成员贡献、最终报告与汇报稿仍需基于完整结果整理。']
    (root/'results/中期进展记录.md').write_text('\n'.join(text)+'\n',encoding='utf8')
    print(json.dumps({'terminal':terminal,'planned':22800,'complete':terminal==22800}),flush=True)
    return summary

if __name__=='__main__':analyze(sys.argv[1])
