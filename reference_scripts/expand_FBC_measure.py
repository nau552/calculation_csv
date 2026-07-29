import csv
import os

BASE = os.path.join(os.path.dirname(__file__), '..', 'result_tmp')

def read_map(path, has_header=False):
    m = {}
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            # allow optional header: if non-numeric first cell, skip
            if has_header and not row[0].strip().isdigit():
                continue
            key = row[0].strip()
            val = row[1].strip() if len(row) > 1 else ''
            m[key] = val
    return m

def load_parameter_label(path):
    d = {}
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (r['InBatchEpoch'].strip(), r['Board'].strip(), r['Chip'].strip(), r['Block'].strip(), r['Measure'].strip())
            d[key] = r
    return d

def load_dataName(path):
    d = {}
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (r['InBatchEpoch'].strip(), r['Board'].strip(), r['Chip'].strip(), r['Block'].strip(), r['Measure'].strip())
            d[key] = r.get('DataName','').strip()
    return d

def main():
    fbc_path = os.path.join(BASE, 'FBC.csv')
    param_path = os.path.join(BASE, 'parameterLabel_FBC.csv')
    dataName_path = os.path.join(BASE, 'dataName_FBC.csv')
    map_dataName_path = os.path.join(BASE, 'map_DataName.csv')
    map_label_path = os.path.join(BASE, 'map_Label.csv')
    map_state_path = os.path.join(BASE, 'map_State.csv')

    map_dataName = read_map(map_dataName_path)
    map_label = read_map(map_label_path)
    map_state = read_map(map_state_path)
    map_override = read_map(os.path.join(BASE, 'map_Override.csv'))

    param_map = load_parameter_label(param_path)
    dataName_map = load_dataName(dataName_path)

    out_path = os.path.join(BASE, 'FBC_expanded.csv')

    with open(fbc_path, newline='', encoding='utf-8') as inf, open(out_path, 'w', newline='', encoding='utf-8') as outf:
        reader = csv.DictReader(inf)
        # Produce exactly the requested columns in this order:
        # InBatchEpoch, Board, Chip, Block, Measure, Erase_Label, Erase_Override,
        # Program_Label, Program_Override, Read_Label, Read_Override, DataName,
        # WL, STR, State, FBC
        fieldnames = ['InBatchEpoch','Board','Chip','Block','Measure',
                      'Erase_Label','Erase_Override','Program_Label','Program_Override',
                      'Read_Label','Read_Override','DataName','WL','STR','State','FBC']
        writer = csv.DictWriter(outf, fieldnames=fieldnames)
        writer.writeheader()

        for r in reader:
            key = (r.get('InBatchEpoch','').strip(), r.get('Board','').strip(), r.get('Chip','').strip(), r.get('Block','').strip(), r.get('Measure','').strip())

            # DataName numeric from dataName_FBC.csv, map to text
            dataName_num = dataName_map.get(key, '')
            dataName_text = map_dataName.get(dataName_num, '') if dataName_num != '' else ''

            param = param_map.get(key, None)
            # parameterLabel_FBC.csv columns: Erase_Label, Erase_Override, Program_Label, Program_Override, Measure, Read_Label, Read_Override, ...
            erase_label_num = param.get('Erase_Label','') if param else ''
            erase_override_num = param.get('Erase_Override','') if param else ''
            prog_label_num = param.get('Program_Label','') if param else ''
            prog_override_num = param.get('Program_Override','') if param else ''
            read_label_num = param.get('Read_Label','') if param else ''
            read_override_num = param.get('Read_Override','') if param else ''

            # map numeric labels to text where possible
            erase_label_text = map_label.get(erase_label_num, '') if erase_label_num != '' else ''
            prog_label_text = map_label.get(prog_label_num, '') if prog_label_num != '' else ''
            read_label_text = map_label.get(read_label_num, '') if read_label_num != '' else ''

            # map overrides to boolean-like True/False (normalize to Title case)
            def map_override_value(code):
                if code == '':
                    return ''
                v = map_override.get(code, code)
                if isinstance(v, str):
                    vv = v.strip().lower()
                    if vv in ('true','t','1'):
                        return 'True'
                    if vv in ('false','f','0'):
                        return 'False'
                # fallback: return original
                return v

            erase_override_text = map_override_value(erase_override_num)
            prog_override_text = map_override_value(prog_override_num)
            read_override_text = map_override_value(read_override_num)

            # map State to text
            state_num = r.get('State','').strip()
            state_text = map_state.get(state_num, '') if state_num != '' else ''

            out_row = {
                'InBatchEpoch': r.get('InBatchEpoch',''),
                'Board': r.get('Board',''),
                'Chip': r.get('Chip',''),
                'Block': r.get('Block',''),
                'Measure': r.get('Measure',''),
                'Erase_Label': erase_label_text,
                'Erase_Override': erase_override_text,
                'Program_Label': prog_label_text,
                'Program_Override': prog_override_text,
                'Read_Label': read_label_text,
                'Read_Override': read_override_text,
                'DataName': dataName_text,
                'WL': r.get('WL',''),
                'STR': r.get('STR',''),
                'State': state_text,
                'FBC': r.get('FBC','')
            }

            writer.writerow(out_row)

    print('Wrote', out_path)

if __name__ == "__main__":
    main()
