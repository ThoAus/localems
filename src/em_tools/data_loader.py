import pandas as pd
import itertools
from tqdm import tqdm

##############################################################################################################

def import_edyna_xlsx_file(path_to_file):
    
    # Read the Excel file with all sheets
    df = pd.read_excel(path_to_file, sheet_name=None, decimal=',')

    # Extract the sheet name to use it later as a column name
    pod = list(df.keys())[0]

    # Take the first sheet
    df = df[pod]

    # Drop the last 4 columns
    df = df.iloc[:, :-4]

    # Convert the first column to datetime
    df['day'] = df['GIORNO']

    # Drop the first column
    df.drop(columns=['GIORNO'], inplace=True)

    # Create a list of all possible time combinations and append 'day' at the end
    hours = ['00', '01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11',
            '12', '13', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23']
    minutes = ['00', '15', '30', '45']
    col_names = [f"{h}{m}" for h, m in itertools.product(hours, minutes)]
    col_names = col_names + ['day']

    # Some xlxs files have a different format for the date column like 'dd/mm/yyyy'
    # Reshape the date column to JJJJMMDD format to ensure consistency
    
    # Check if the date is in 'dd/mm/yyyy' format
    if df['day'].dtype == 'object' and '/' in df['day'].iloc[0]:
        df['day'] = df['day'].apply(lambda day: f"{day.split('/')[2]}{day.split('/')[1]}{day.split('/')[0]}" if '/' in day else day)

    # Rename the columns
    df.columns = col_names

    return df, pod

##############################################################################################################

def reshape_monthly_data_to_long(df, pod):
    
    df_long = df.melt(id_vars=['day'], var_name='hour', value_name=pod)
    
    # Convert the 'day' and 'hour' columns to datetime
    df_long['time'] = df_long['day'].astype(str) + ' ' + df_long['hour']
    try:
        df_long['time'] = pd.to_datetime(df_long['time'], format='%Y%m%d %H%M')
    except:
        df_long['time'] = pd.to_datetime(df_long['time'], format='%d/%m/%Y %H%M', errors='coerce')

    # Check for NaT values and print a warning if any are found
    if df_long['time'].isna().any():
        print(f'Problem with {pod} data: some time values could not be parsed. Please check the input data format.')

    # Set the 'time' column as index
    df_long.set_index('time', inplace=True)
    
    # Drop the 'day' and 'hour' columns
    df_long.drop(columns=['day', 'hour'], inplace=True)
    
    # Sort the index
    df_long.sort_index(inplace=True)
    
    return df_long

##############################################################################################################

def combine_header_and_data(
        header_df: pd.DataFrame,
        data_df: pd.DataFrame,
        ):
    """
    Combines the header DataFrame with the data DataFrame by matching PODs.
    Args:
        header_df (pd.DataFrame): DataFrame containing the PODs and further information as a multi-index.
        data_df (pd.DataFrame): DataFrame containing the data for each POD.
    Returns:
        pd.DataFrame: A new DataFrame with the multi-index from the header DataFrame and the data from the data DataFrame.
    """

    for pod_tuple in tqdm(header_df.columns):
        pod = pod_tuple[-1]  # Extract the POD string from the tuple

        # Check if the POD is in the consumption DataFrame
        if pod in data_df.columns:
            # Get the data for the POD
            pod_data = data_df[pod]
            # Add the data to the new DataFrame
            header_df[pod_tuple] = pod_data.values
        else:
            print(f"POD {pod} not found in consumption data.")

    # Add the datatime index from the consumption DataFrame to the pods_df DataFrame
    header_df.index = data_df.index

    return header_df

##############################################################################################################

def select_columns_by_pod(
        df: pd.DataFrame,
        pod: list,
        ):
    """
    Selects columns from a DataFrame based on the provided list of PODs.
    Args:
    - df: DataFrame with a multi-index column structure
    - pod: list of PODs to select from the DataFrame
    Returns:
    - series: A Series containing the sum of the selected POD columns across all other dimensions.
    """
    try:
        df_selected = df.loc[:, (slice(None), slice(None), slice(None), pod)] # type: ignore
    except:
        raise ValueError("PODs not found in columns or problem with mulit-index.")

    return df_selected.sum(axis=1)